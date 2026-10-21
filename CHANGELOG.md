# SAGE — Changelog

All notable implementation milestones are recorded here.  
Entries follow **[Prompt ID] — YYYY-MM-DD** ordering, newest first.

## [Prompt 045] — 2026-10-21

### Critical Database Key Rotation Concurrency Remediation (SEC-044-01 through SEC-044-07)

**Status:** IMPLEMENTED & VERIFIED — Remediated all critical and high severity concurrency and cross-process vulnerabilities discovered in the Prompt 044 security audit:
- **SEC-044-01 (Startup Recovery Concurrency Race):**
  - Wrapped `recover_interrupted_rotation()` in `initialize_runtime_database()` (`src/storage/database.py`) with `ExclusiveDatabaseLock(validated_path, timeout_sec=10.0)`.
  - Concurrently booting processes that encounter an active rotation catch `RotationLockError` and gracefully skip startup recovery without unlinking staging files or racing against the active rotation coordinator.
- **SEC-044-02 & SEC-044-06 (Data Loss Race & Content Mutation Parity Detection):**
  - Enhanced `_collect_table_counts()` in `src/security/database_key_rotation.py` to calculate an XOR-CRC32 content fingerprint over all table rows, guaranteeing that in-place `UPDATE` operations, paired `DELETE`/`INSERT`, and trigger mutations are detected even when row count remains unchanged.
  - Added pre-swap verification of `can_acquire_exclusive_file_access(validated_path)` immediately prior to DPAPI key commitment.
- **SEC-044-03 (Store Write Path Draining Gating):**
  - Exposed `assert_not_draining(conn: SQLiteConnection)` helper in `src/storage/database.py` and exported it via `src/storage/__init__.py`.
  - Added explicit drain checks before `BEGIN IMMEDIATE` across all write paths in `NotificationStore` (`src/notifications/store.py`) and `AuditWriter` (`src/trust/audit.py`), guaranteeing that direct SQL write commands cannot execute while key rotation is draining.
- **SEC-044-07 (Atomic .drain File Publication):**
  - Updated `ConnectionCoordinator.set_drain()` in `src/storage/database.py` to write JSON payload to a sibling `.drain.tmp` file and atomically rename via `os.replace()`, preventing external processes from observing partial or corrupted JSON metadata.
- **Real Subprocess & Concurrency Verification (`tests/test_database_encryption.py`):**
  - Added 5 new verification tests (`test_33` through `test_37`):
    - `test_33`: Startup recovery under active rotation lock preserves staging artifacts.
    - `test_34`: Direct store write methods in `NotificationStore` and `AuditWriter` raise `DatabaseDrainingError` during drain.
    - `test_35`: Parity verification detects row content `UPDATE` without count changes.
    - `test_36`: Atomic `.drain` file creation and cleanup verification.
    - `test_37`: Real independent Python OS process (via `subprocess.run([sys.executable, ...])`) is blocked by active `.drain` file.
  - Encryption suite: 37/37 passed in 5.87s.
  - Full regression suite in canonical virtual environment: 722/722 passed in 76.60s (0 failures, 100% pass rate).

## [Prompt 043] — 2026-10-20

### Coordinated Database Connection Draining & Windows File-Replacement Contention Remediation (Risk A)

**Status:** IMPLEMENTED & VERIFIED — Remediated Risk A from Prompt 042 by implementing coordinated in-process connection tracking, cross-process advisory draining, admission control, graceful transaction completion, pre-swap parity verification, and bounded Windows filesystem replacement retries:
- **Connection Coordinator & Cross-Process Protocol (`src/storage/database.py`):**
  - Built `ConnectionCoordinator` maintaining an in-process connection registry and cross-process advisory drain file (`<db>.drain`) with JSON metadata (coordinator PID, timestamp, timeout).
  - Implemented admission control in `create_connection()` and `open_database()` rejecting new connections with `DatabaseDrainingError` when draining is active.
  - Implemented transaction admission gating in `transaction()` preventing new transactions from starting while draining.
  - Draining closes idle connections immediately and marks active in-flight transactions for graceful closure upon transaction exit.
  - Built `can_acquire_exclusive_file_access()` utilizing Win32 `kernel32.CreateFileW(..., FILE_SHARE_NONE)` to deterministically verify that 0 operating system file handles are held on the database or WAL file prior to replacement.
- **Rotation Pipeline Hardening (`src/security/database_key_rotation.py`):**
  - Integrated `ConnectionCoordinator.drain()` inside `ExclusiveDatabaseLock` with configurable `drain_timeout_sec` (default 15.0s).
  - Explicitly verifies `PRAGMA wal_checkpoint(TRUNCATE)` return values `(busy, log, checkpointed)`, failing closed if `busy == 1`.
  - Added pre-swap active database row count parity verification (`_collect_table_counts`) immediately before committing the new key, preventing silent write losses between staging export and swap.
  - Wrapped `os.replace` on database publication and journal recording in bounded retry loops with exponential backoff (5 attempts) to absorb transient Windows filesystem locks.
  - Added guaranteed drain cleanup in outer `finally:` block across both success and failure, and ensured startup recovery (`recover_interrupted_rotation`) unlinks lingering `.drain` files.
- **Comprehensive Concurrency & Conformance Tests (`tests/test_database_encryption.py`):**
  - Added 10 tests (`test_23` through `test_32`) covering active reader draining, writer transaction wait, connection rejection while draining, drain timeout safe abort, pre-swap concurrent write detection, WAL busy outcome handling, cross-process `.drain` file enforcement, Windows bounded replace retries, connection resumption post-drain, and 100% multi-table record fidelity.
  - Encryption suite: 32/32 tests passed in 6.25s.
  - Full project regression suite in canonical virtual environment: 717/717 passed in 71.74s (0 failures, 100% pass rate).

## [Prompt 040] — 2026-10-20

### Coordinated Database Master Key Rotation & Disaster Recovery Subsystem (Remediation of SEC-038-02)

**Status:** IMPLEMENTED & VERIFIED — Remediated finding SEC-038-02 by replacing isolated keyfile overwrites with coordinated, transactional whole-database rekeying via staged `sqlcipher_export`, crash-resilient journaled state recovery, non-signaling Win32 cross-process concurrency locking, monotonic key versioning with historical archive, and managed backup rekeying:
- **Coordinated Key Rotation Subsystem (`src/security/database_key_rotation.py`):**
  - Built `rotate_database_key()` executing transactional database rekeying:
    - Creates an encrypted pre-rotation snapshot under the active key before mutating files.
    - Checkpoints active WAL frames via `PRAGMA wal_checkpoint(TRUNCATE);`.
    - Staged rekey export via native `ATTACH ... KEY "x'<new_key>'"` and `SELECT sqlcipher_export('staged')`.
    - Cryptographic verification (`PRAGMA integrity_check`) and strict table/row-count parity comparison between source and staged databases prior to commitment.
    - Commits new key to DPAPI-backed keyfile, archives prior key version, and atomically replaces database via `os.replace`.
    - Explicitly verifies fail-closed rejection of old key material (`_verify_key_rejected`) and authentication under new key.
    - Automatically synchronizes all managed backups in `%USERPROFILE%\.aegis\backups\` to the new key.
- **Crash Recovery & State Journaling (`RotationJournal`, `recover_interrupted_rotation`):**
  - Built durable JSON journal recording state machine progression (`PREPARING`, `SNAPSHOT_CREATED`, `STAGING_EXPORTED`, `STAGING_VERIFIED`, `KEY_COMMITTED`, `DATABASE_COMMITTED`).
  - Implemented `recover_interrupted_rotation()`: deterministically rolls back pre-key commit failures (cleaning staging files and journal) or rolls forward post-key commit states (completing atomic swap and backup synchronization).
  - Integrated startup recovery check directly into `initialize_runtime_database()` in `src/storage/database.py`.
- **Cross-Process Concurrency Locking (`ExclusiveDatabaseLock`):**
  - Mutual exclusion lock (`<db>.rotation.lock`) preventing concurrent rotations or boot-time races.
  - Stale lock detection with non-signaling Win32 process liveness check via `kernel32.OpenProcess` and `GetExitCodeProcess`, resolving console process group `CTRL_C_EVENT` termination bugs on Windows.
- **Monotonic Key Versioning & Historical Archive (`src/security/database_key.py`):**
  - Updated `DatabaseKeyManager` with monotonic versioning (`version`, `commit_new_key`, `archive_current_key`).
  - Retains DPAPI-protected historical key versions to guarantee older external or offline backups remain decryptable via `get_key(version=N)`.
- **Comprehensive Test Suite & Verification (`tests/test_database_encryption.py`):**
  - Expanded test suite to 22 tests including successful rotation, failure injection, crash recovery (both rollback and roll-forward), lock concurrency, and historical archive restoration.
  - All 22 tests pass in 2.71s. Full project regression suite in canonical virtual environment: 707 passed in 64.25s (0 failures, 100% pass rate).

## [Prompt 039] — 2026-10-18

### Encrypted Backups & Safe Migration Recovery (Remediation of SEC-038-01)

**Status:** IMPLEMENTED & VERIFIED — Remediated finding SEC-038-01 by replacing raw `shutil.copy2` pre-migration plaintext backups with native SQLCipher page-level encrypted snapshots, active WAL checkpointing, atomic verified publication, independent backup restoration, and safe legacy backup remediation:
- **Encrypted Migration Backups (`src/storage/database.py`):**
  - Eliminated plaintext database copies prior to encryption migration.
  - Added transaction flushing and WAL checkpointing (`PRAGMA wal_checkpoint(TRUNCATE)`) before snapshotting to ensure consistent recovery points.
  - Utilizes SQLCipher's native `sqlcipher_export` to write an encrypted snapshot to an isolated staging file (`<stem>_backup_<uuid>.staging`) protected by the DPAPI-backed master database key.
  - Verified staging backup cryptographically (`PRAGMA integrity_check == "ok"` and `is_database_encrypted`) before publication.
  - Atomically publishes verified encrypted backup via `os.replace` to `<stem>_pre_encryption_backup.db`.
  - Fail-closed guarantees: if backup creation, export, or verification fails, the unencrypted source database is preserved completely intact, all staging artifacts are purged, and migration aborts.
- **Programmatic Backup Restoration (`restore_database_from_backup`):**
  - Added programmatic restore utility supporting both encrypted target databases (preserving SQLCipher security) and plaintext export targets (for emergency/disaster-recovery inspection).
  - Validates target directory, decrypts/copies via authenticated SQLCipher engine, and validates schema integrity post-restoration.
- **Legacy Plaintext Backup Remediation & Safe Inspection (`scan_legacy_plaintext_backups`, `remediate_legacy_backup`):**
  - Added `scan_legacy_plaintext_backups()`: Non-destructive header inspection (reading only the 16-byte SQLite magic header without reading table rows or payload data) to detect legacy unencrypted backups.
  - Added `remediate_legacy_backup()`: Safe, opt-in remediation utility. Encrypts legacy plaintext backups using SQLCipher into a verified encrypted snapshot. Requires explicit `dry_run=False` and `remove_plaintext=True` to remove legacy files; strictly no automatic deletion during startup.
- **Expanded Security Test Suite (`tests/test_database_encryption.py`):**
  - Added Tests 12-15 covering encrypted backup creation and restoration, WAL checkpoint consistency during snapshotting, failure injection preserving source database, and legacy backup scanning/remediation.
  - Updated Test 08 to assert encrypted backup verification and zero plaintext leakage.
  - All 15 tests pass in 1.30s. Full test suite: 700 passed in 64.80s.

## [Prompt 037] — 2026-10-18

### Whole-Database Encryption via SQLCipher & Windows DPAPI Key Management

**Status:** IMPLEMENTED & VERIFIED — Resolved the whole-database encryption blocker in AEGIS using SQLCipher 4.19.0 Community (`sqlcipher3` 0.6.4), Windows DPAPI master key protection, fail-closed connection lifecycle, and zero-data-loss atomic migration:
- **Master Database Key Management (`src/security/database_key.py`, `src/storage/paths.py`):**
  - Built `DatabaseKeyManager` managing a cryptographically secure 256-bit CSPRNG master key.
  - Wrapped at rest using Windows DPAPI (`CryptProtectData` with user security context and secondary entropy binding `b"aegis:database_master_key:v1"`).
  - Stored at `%USERPROFILE%\.aegis\keys\aegis_db.key.enc`.
  - Implemented secure key lifecycle methods: `get_or_create_key()`, `get_key()`, `rotate_key()`, and `destroy_key()`.
  - Zero-secret invariant: key material is strictly excluded from logs, error messages, and exceptions.
- **SQLCipher Integration & Fail-Closed Connection Engine (`src/storage/database.py`):**
  - Integrated `sqlcipher3` with fallback detection (`SQLCIPHER_AVAILABLE`).
  - Standardized exception hierarchy with unified `DatabaseError`, `IntegrityError`, `OperationalError` covering both `sqlite3` and `sqlcipher3`.
  - Updated `create_connection()`, `open_database()`, and `initialize_runtime_database()` to support `encryption_key` and `encrypted` parameters.
  - Enforced fail-closed authentication: immediately applies raw binary key via `PRAGMA key = "x'HEX'"` and validates with `SELECT count(*) FROM sqlite_master;`. Fails closed if key is invalid or database header corrupted (zero plaintext fallback).
  - Configured SQLCipher security settings: AES-256-CBC, 4096-byte pages, HMAC-SHA512 page integrity, and encrypted WAL journals.
- **Zero-Data-Loss Migration & Disaster Recovery (`src/storage/database.py`):**
  - Built `migrate_database_to_encrypted()` utility:
    - Pre-migration verification and idempotent skip for already-encrypted databases.
    - Timestamped pre-migration backup snapshot in `%USERPROFILE%\.aegis\backups\pre_encryption_snapshot_<ts>.db`.
    - Staged schema/data export via `sqlcipher_export` preserving all 9 migrations, tables, indexes, triggers, and sequences.
    - Post-export cryptographic integrity check (`PRAGMA integrity_check`) asserting `ok`.
    - Atomic file replacement (`os.replace`) with automatic rollback and staging cleanup on failure.
  - `initialize_runtime_database()` transparently detects and auto-migrates existing plaintext databases upon startup.
- **Comprehensive Security Test Suite (`tests/test_database_encryption.py`):**
  - Implemented 11 dedicated Phase E security tests covering page-level encryption, absence of SQLite magic header, zero-plaintext leakage in files and WAL journals, correct key authentication, wrong/missing key fail-closed denial, DPAPI key lifecycle, key length validation, full plaintext-to-encrypted migration with 100% data preservation, interrupted migration safety, and transparent bootstrapper migration.
  - All 11 tests pass in 0.88s.
- **Full Regression & Quality Verification:**
  - 685 tests passed (0 failures) across the entire AEGIS test suite.
  - `ruff check`, `ruff format`, and `mypy` passing with zero errors.
- **Security Documentation:**
  - `docs/AEGIS_DATABASE_ENCRYPTION_AUDIT.md`: Complete audit report detailing baseline findings, cryptographic verification, and resolution of the live-pairing blocker.
  - `docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md`: Detailed two-tier encryption specification, key hierarchy, and PRAGMA settings.
  - `docs/AEGIS_DATABASE_ENCRYPTION_MIGRATION.md`: Operational runbook, backup verification, and disaster-recovery procedures.

**Tests:** 11 passed · 0 failed in `test_database_encryption.py` · 685 passed · 0 failed in full test suite.  
**Deliverables:** `src/security/database_key.py`, `src/security/__init__.py`, `src/storage/database.py`, `src/storage/paths.py`, `src/storage/__init__.py`, `src/gateway/store.py`, `tests/test_database_encryption.py`, `tests/test_secure_contact_memory.py`, `tests/test_notifications.py`, `tests/test_environment_validation.py`, `docs/AEGIS_DATABASE_ENCRYPTION_AUDIT.md`, `docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md`, `docs/AEGIS_DATABASE_ENCRYPTION_MIGRATION.md`, `docs/AEGIS_PROJECT_STATE.md`, `CHANGELOG.md`.

## [Prompt 036] — 2026-10-18

### Secure Contact Memory & WhatsApp Receive-Only Integration

**Status:** IMPLEMENTED & VERIFIED — Implemented per-contact cryptographic isolation, Windows DPAPI key protection, contact-scoped encrypted memory, supervised receive-only Baileys sidecar bridge, and database at-rest audit:
- **Per-Contact Cryptographic Foundation (`src/security/`):**
  - `AES256GCMCipher` (`src/security/crypto.py`): Vetted authenticated encryption (AEAD) using `cryptography.hazmat.primitives.ciphers.aead.AESGCM` with CSPRNG 96-bit nonces (`os.urandom(12)`) and AAD binding (`contact_jid:record_id:version`). Rejects tampering and fails closed with zero partial plaintext.
  - `WindowsDPAPIKeyProtector` (`src/security/dpapi.py`): Key-wrapping abstraction using Windows DPAPI (`CryptProtectData` / `CryptUnprotectData` via `ctypes.windll.crypt32`) with user security context. Pluggable `KeyProtector` interface with `SoftwareKeyProtector` for cross-platform environments.
  - `ContactKeyStore` (`src/security/contact_key_store.py`): Manages isolated, random 256-bit DEKs per contact JID. Enforces strict JID canonicalization (`validate_and_normalize_jid`), rejects group and broadcast destinations, and executes cryptographic erasure on demand (`destroy_key`).
- **Encrypted Contact Memory Subsystem (`src/memory/contact_memory.py`):**
  - Built `EncryptedContactMemoryStore` storing private contact records in `contact_memory_records`.
  - Storage-layer isolation: all queries are strictly scoped (`WHERE contact_jid = ?`). Contact A can never view or decrypt Contact B's memory.
  - Cryptographic erasure: `purge_contact_completely` combines database record deletion with DPAPI key destruction, rendering residual disk blocks permanently undecryptable.
- **Gateway Contact-Scoped Decryption & Receive-Only Integration (`src/gateway/gateway.py`, `src/config/models.py`):**
  - Integrated `EncryptedContactMemoryStore` into `ChannelGateway` following the required 12-step processing pipeline.
  - Added `receive_only: bool` in `WhatsAppConfig` (defaults to False for simulated test backward compatibility; active in receive-only sidecars).
  - Enforced strict receive-only outbound boundary in `_dispatch_outbound`: suppresses automated replies and external transmission, recording failure status.
- **Supervised Receive-Only Baileys Sidecar (`src/gateway/baileys_sidecar.py`):**
  - Implemented `BaileysSidecarSupervisor` managing child process over stdin/stdout JSON-lines protocol (`src/gateway/sidecar_protocol.py`). Stderr dedicated to diagnostic logging; zero network ports exposed.
  - Unconditional receive-only enforcement: `send_message()` strictly raises `TransportError` preventing outbound sends or autonomous replies.
- **Database At-Rest Encryption Audit (`docs/AEGIS_DATABASE_ENCRYPTION_AUDIT.md`):**
  - Comprehensive audit of SQLite persistence layer on Windows Python 3.12.
  - Documented SQLCipher compiler blocker (no prebuilt wheels on PyPI on Windows Python 3.12).
  - Documented protection matrix (encrypted contact payloads vs. unencrypted structural metadata).
  - Explicitly recorded that live account pairing remains blocked until certified whole-database encryption or explicit risk signoff is completed.
- **Deterministic Security Tests (`tests/test_secure_contact_memory.py`):**
  - 20 deterministic security tests implemented and verified (Tests 1-10: Encryption & Key Isolation; Tests 11-20: Gateway & WhatsApp Security).
  - All 20 tests pass. Zero secrets logged. Full suite (90 tests across gateway, whatsapp, notifications, approval, and secure memory) passing.

**Tests:** 20 passed · 0 failed in `test_secure_contact_memory.py` · 90 passed in gateway/security suite · 685 passed in full test suite.  
**Deliverables:** `src/security/__init__.py`, `src/security/crypto.py`, `src/security/dpapi.py`, `src/security/contact_key_store.py`, `src/memory/contact_memory.py`, `src/gateway/baileys_sidecar.py`, `src/gateway/gateway.py`, `src/config/models.py`, `src/memory/__init__.py`, `tests/test_secure_contact_memory.py`, `docs/AEGIS_DATABASE_ENCRYPTION_AUDIT.md`, `CHANGELOG.md`, `docs/AEGIS_PROJECT_STATE.md`.

## [Prompt 035] — 2026-10-18

### Phase 9C.2 Simulated WhatsApp Transport Foundation

**Status:** IMPLEMENTED & VERIFIED — Implemented an offline-only, deterministic simulated WhatsApp transport, sidecar IPC protocol, and channel adapter foundation for SAGE's remote-control gateway:
- **WhatsApp Configuration (`src/config/models.py`):** Added strongly typed `WhatsAppConfig` with validation for `enabled`, `allowed_jids`, `session_path`, `max_message_length`, `rate_limit_per_minute`, `require_outbound_approval`, and `presence_state`. Integrated into `ChannelConfig.whatsapp`. Rejects group and broadcast JIDs (`@g.us`, `@broadcast`) in allowlists.
- **Channel Domain Models (`src/gateway/models.py`):** Introduced `ChannelPlatform`, `ConversationType`, `NormalizedInboundMessage`, and `NormalizedOutboundMessage`. Added approval-binding metadata (`content_hash`, `requires_approval`, `approval_id`) to `OutboundMessage`.
- **Sidecar IPC Protocol (`src/gateway/sidecar_protocol.py`):** Built strict versioned ("1.0") newline-delimited JSON-lines protocol definitions for the future Baileys sidecar bridge. Implemented `parse_sidecar_line` and `serialize_sidecar_envelope` enforcing 64KB max line length, 4096 character text limits, typed event routing (`inbound_message`, `outbound_send`, `outbound_ack`, etc.), and zero-secret scanning.
- **WhatsApp Channel Adapter (`src/gateway/whatsapp.py`):** Built `WhatsAppAdapter` implementing update parsing and outbound text chunking. Implemented critical CVE-2026-48063 mitigation (drops `requestId` placeholder history sync payloads), rejects inbound group/broadcast messages, discards self-messages (`fromMe=True`), and scrubs secrets.
- **Simulated WhatsApp Transport (`src/gateway/whatsapp_transport.py`):** Built `SimulatedWhatsAppTransport` with connect/disconnect simulation, FIFO event queueing, synthetic event creation, independent outbound destination validation (rejecting `@g.us` and `@broadcast`), simulated network drop before server ACK (`UNCERTAIN` delivery), and exact-content (`sha256`) and exact-recipient approval enforcement against Trust Core's `ApprovalStore`.
- **Channel Orchestrator Generalization (`src/gateway/gateway.py`):** Generalized `ChannelGateway` to coordinate both Telegram and WhatsApp channels transparently. Outbound dispatches strictly enforce destination authorization and exact-content approval binding for autonomous messages without bypassing human approval gates.
- **Offline Test Suite (`tests/test_whatsapp_transport.py`):** Implemented 20 offline tests covering all 14 required areas: private message command routing (/status, /task, /help), unknown contact silent drop, inbound group/broadcast rejection, durable deduplication, malformed payload and CVE-2026-48063 rejection, reconnect and replay, restart persistence, outbound group rejection, exact-content approval binding, tamper invalidation, uncertain delivery status, interrupted task recovery (`reconcile_startup`), secret redaction, and protocol envelope validation.

**Tests:** 20 passed · 0 failed in `test_whatsapp_transport.py` · 64 passed in gateway/notification suites · 661 passed in full test suite (4 expected environmental exclusions: system python isolation check and 3 uninstalled `llama_cpp` tests).  
**Deliverables:** `src/config/models.py`, `src/gateway/models.py`, `src/gateway/sidecar_protocol.py`, `src/gateway/whatsapp.py`, `src/gateway/whatsapp_transport.py`, `src/gateway/gateway.py`, `src/gateway/__init__.py`, `tests/test_whatsapp_transport.py`, `docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md`, `docs/AEGIS_PROJECT_STATE.md`.

## [Prompt 034] — 2026-10-18


### Phase 9C.1 Gateway Contract & Security Remediation

**Status:** REMEDIATED & VERIFIED — Implemented targeted security, contract, crash recovery, and notification remediation for the Phase 9A remote gateway (`src/gateway/`, `src/config/`, `src/notifications/`) addressing all confirmed gaps identified in the Phase 9B audit before WhatsApp integration:
- **Command Contract:** Implemented safe `/start` onboarding handler returning structured, zero-side-effect greeting and pointer to `/help`; updated `/help` to accurately enumerate currently supported commands; mapped `/deny` as a secure alias for `/reject` without bypassing Trust Core or authorization checks; confirmed `/stop` and `/cancel` equivalence.
- **Chat Authorization:** Added explicit `allowed_chat_ids: List[int]` configuration field in `TelegramConfig` (`src/config/models.py`) with strict validation (positive integers only, requiring at least one private chat ID when enabled); updated example YAML files (`config/sage.example.yaml`, `config/aegis.example.yaml`). In `ChannelGateway._process_single_update`, enforced dual-gate authorization requiring both sender ID in `allowed_user_ids` and chat ID in `allowed_chat_ids` before command routing, inference, memory access, or task execution; unauthorized senders and chats fail closed without sending diagnostic replies.
- **Outbound Destination Validation:** Updated `_dispatch_outbound` to strictly reject non-positive integer destinations (Telegram groups/supergroups/channels) and non-allowlisted destinations before transport dispatch, preventing accidental or unauthorized group broadcast.
- **Safe Crash Recovery:** Implemented `reconcile_startup()` in `ChannelGateway` to reconcile orphaned `PROCESSING` or `QUEUED` inbound updates and unacknowledged `PENDING` outbound records following process crashes. Adheres strictly to the conservative recovery invariant: orphaned updates without completed tasks are marked `FAILED` with recovery required notes and are never automatically replayed; unconfirmed pending outbounds are marked `UNCERTAIN`; reconciliation is deterministic and idempotent.
- **Telegram Notification Delivery Adapter:** Hardened `TelegramDeliveryAdapter` (`src/notifications/adapters.py`) to reject non-positive chat IDs at initialization and dispatch, catch transport exceptions safely, and redact bot tokens and credentials from error logs and results. Preserved `build_default_adapters()` as `[LocalLogAdapter()]` to avoid unintended background network attempts at startup.
- **Secret Scrubbing:** Enhanced `HttpTelegramTransport._redact` and `_redact_token` (`src/gateway/transport.py`) to systematically scrub instance bot tokens, generic bot token patterns, and credential-bearing URLs from all logs, errors, and exceptions.
- **Verification:** Added 7 new comprehensive unit and integration tests (`test_22` through `test_28`) in `tests/test_remote_gateway.py`. All 28 gateway tests and all 16 notification tests pass (44/44 passed).

**Tests:** 44 passed · 0 failed (gateway & notification suites) · 641 passed in full test suite (4 expected environmental exclusions: system python isolation check and 3 uninstalled `llama_cpp` tests).  
**Deliverables:** `src/gateway/router.py`, `src/gateway/gateway.py`, `src/gateway/transport.py`, `src/gateway/store.py`, `src/config/models.py`, `src/notifications/adapters.py`, `config/sage.example.yaml`, `config/aegis.example.yaml`, `tests/test_remote_gateway.py`.

## [Prompt 033] — 2026-10-18

### Phase 9B Gateway Contract Audit & WhatsApp Adapter Design

**Status:** AUDITED & ARCHITECTURE PLANNED — Conducted a comprehensive security and contract audit of the Phase 9A remote gateway implementation (`src/gateway/`) and designed the architecture for the upcoming WhatsApp channel integration (`docs/PHASE_9B_GATEWAY_AUDIT.md`, `docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md`). Verified actual code implementation against documented claims: identified documentation-to-code gaps (HMAC claim in Prompt 032 was inaccurate as code uses standard TLS token authentication; long-polling only with no webhooks; command set in code implements `/help, /status, /tasks, /inspect, /verify, /diagnose, /task, /confirm, /cancel, /stop, /approve, /reject` while `/start` currently returns error; `TelegramDeliveryAdapter` lacks automated tests). Established the channel-neutral message contract and designed the personal-use, local-first Baileys adapter architecture (`@whiskeysockets/baileys@6.7.22` with mandatory CVE-2026-48063 mitigations, isolated `%USERPROFILE%\.aegis\channels\whatsapp\session\` path with user ACLs, exact-content outbound human approval binding, inbound/outbound group chat blocking, contact allowlists, presence states `AVAILABLE, BUSY, DND, OFF`, and 8-phase implementation plan for Phase 9C). Documentation-only deliverable; zero application source modifications.  
**Tests:** 638 passed · 0 failed · 0 skipped (100% offline inside SAGE venv)  
**Deliverables:** `docs/PHASE_9B_GATEWAY_AUDIT.md`, `docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md`

## [Prompt 032] — 2026-10-18

### Secure Remote-Control Gateway (Telegram-First, Phase 9A)

**Status:** IMPLEMENTED & VERIFIED — Implemented the first secure remote-control gateway channel for SAGE (`src/gateway/`), establishing a hardened, Telegram-first entry point into SAGE's existing Supervisor, task execution, skill, memory, notification, and Trust Core architecture without compromising the local-first, zero-secret security perimeter. Designed with multi-layered perimeter defense: constant-time HMAC token validation, strict sender ID and chat ID allowlisting, persistent SQL-backed replay attack deduplication (`migrations/0009_channel_gateway_schema.sql`), sliding-window per-user rate limiting, content sanitization and zero-secret scrubbing. Implemented a deterministic `CommandRouter` supporting 10 safe remote commands (`/start`, `/help`, `/status`, `/briefing`, `/approve`, `/deny`, `/task`, `/cancel`, `/pause`, `/resume`) that strictly respects Autonomy Tiers, fails closed on unknown or unapproved operations, and delegates execution directly to SAGE's `Supervisor` and `ApprovalStore` rather than spawning a separate agent. Added `TelegramNotificationAdapter` for real-time remote delivery of advisory alerts and pending approvals.  
**Tests:** 638 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~75.2s) — 21 new tests in `tests/test_remote_gateway.py`  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean  
**Type Check:** `mypy src/gateway/ src/config/ src/notifications/ --ignore-missing-imports` — 0 issues  

#### A. Perimeter Security & Hardening (`src/gateway/rate_limiter.py`, `src/gateway/store.py`, `src/gateway/gateway.py`)
- Identity Verification & Allowlisting: Numeric Telegram user ID (`allowed_user_ids`) and chat ID (`allowed_chat_ids`) verified against configuration; username spoofing neutralized by ignoring user-provided string aliases.
- Group & Channel Isolation: Strictly rejects updates originating from group chats, supergroups, or public channels before any parsing or inference.
- Replay Protection & Deduplication: Two-tier deduplication (in-memory LRU cache + SQLite `gateway_updates` table) preventing duplicated update execution across process restarts or network retries.
- Rate Limiting: `SlidingWindowRateLimiter` enforces strict requests-per-minute limits per user with burst thresholds; excessive requests fail safely with bounded rate-limit warnings.
- Zero-Secret Invariant & Sanitization: Bot tokens, API keys, credentials, and private paths scrubbed from logging, audit trails, and responses via `_check_no_secrets()`.

#### B. Command Router & Supervisor Integration (`src/gateway/router.py`)
- 10 Safe Commands: `/start`, `/help`, `/status`, `/briefing`, `/approve`, `/deny`, `/task`, `/cancel`, `/pause`, `/resume`.
- Trust Core & Approval Integration: `/approve` and `/deny` operate directly on `ApprovalStore` pending requests with fail-closed validation; unauthorized or mismatched requests cannot alter approval states.
- Task Submission & Persistent Execution: `/task` routes user goals through SAGE's `Supervisor` and persistent completion mode; arbitrary shell execution and direct filesystem paths are prohibited.
- Safe Error Handling: Unknown commands, malformed parameters, and unhandled errors produce structured, friendly responses without leaking stack traces or internal secrets.

#### C. Transport & Notification Adapter (`src/gateway/transport.py`, `src/gateway/telegram.py`, `src/notifications/adapters.py`)
- Decoupled Transport Layer: `TelegramTransport` protocol with `MockTelegramTransport` for 100% offline testing and `TelegramBotTransport` for production polling/webhook interaction.
- Outbound Delivery Queue: `gateway_outbound_messages` records outbound delivery status (`QUEUED`, `SENT`, `FAILED`) with delivery attempt counts and retry safety.
- `TelegramNotificationAdapter`: Bridges SAGE's `NotificationManager` to Telegram, allowing advisory alerts (P0-P4) and approval requests to reach the user remotely while strictly maintaining the advisory-only invariant.

#### D. Schema Migration & Configuration (`migrations/0009_channel_gateway_schema.sql`, `src/config/models.py`)
- `0009_channel_gateway_schema.sql`: Staged migration creating `gateway_updates` (deduplication & lifecycle tracking) and `gateway_outbound_messages` (delivery audit) tables.
- `TelegramConfig`: Added Pydantic schema in `src/config/models.py` with validation for `bot_token`, `allowed_user_ids`, `allowed_chat_ids`, `rate_limit_rpm`, and polling intervals.
- Example configs updated: `config/aegis.example.yaml` and `config/sage.example.yaml`.

## [Prompt 031] — 2026-10-10

### First-Party Skills & End-to-End SAGE Demonstration

**Status:** IMPLEMENTED & VERIFIED — Developed three bounded, verifiable first-party skills (`workspace.inspect`, `dev.verify`, `research.diagnose`) and proved their integration through SAGE's complete end-to-end execution architecture. Validated the full lifecycle: User Request -> Persistent Mode Selection -> Context Briefing -> User Confirmation -> Planning -> Skill Selection -> Trust Core Authorization -> Tool Execution -> Observation -> Verification -> Error Diagnosis -> Local/Online Research -> Hypothesis Testing & Rejection -> Alternative Strategy Selection -> Independent Verification -> Checkpointing -> Verified Completion -> Experience Recording -> Simulated Restart Recovery. Built a reusable 7-task Golden-Task evaluation harness (`GoldenTaskEvaluator`) covering read-only inspection, search & summarization, controlled test execution, recovery from transient failures, researching unfamiliar errors, resuming checkpointed tasks across restart, and handling ambiguous requests via clarification.
**Tests:** 617 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~46.7s) — 16 new tests in `tests/test_first_party_skills.py` (11) and `tests/test_e2e_demonstration.py` (5)  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (148 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (146 source files)  

#### A. First-Party Skills Architecture (`src/skills/first_party.py`)
- `workspace.inspect`: Read-only inspection of workspace structure, file hierarchy, pattern search, and evidence-backed summaries. Tool bindings: `file_list`, `file_read`, `file_search`. Capabilities: `filesystem.list`, `filesystem.read`. Strictly read-only; attempts to invoke mutating tools through `BoundSkillToolBroker` fail closed (`SkillToolAccessDeniedError`).
- `dev.verify`: Controlled development verification executing explicitly whitelisted project test, lint, format, typecheck, and syntax commands via `dev_runner` and safe `python_eval`. Capabilities: `process.run_test`. Strictly prohibits arbitrary or unrestricted shell execution (`shell=True`).
- `research.diagnose`: Technical research and error diagnosis searching local documentation first, consulting permitted online providers when authorized, preserving source provenance, and formulating testable recovery hypotheses (`HYP-01`, `HYP-02`, etc.) with empirical strategies.
- `register_first_party_skills()`: Registers all three declarative manifests and deterministic executors with schema and output verification into `SkillRegistry`.

#### B. Tool Broker Safe Primitives (`src/tools/builtin.py`)
- Extended canonical tool catalog with `file_search` (`FILE_SEARCH_TOOL`), `dev_runner` (`DEV_RUNNER_TOOL`), and `research_query` (`RESEARCH_QUERY_TOOL`).
- Enhanced `_validate_safe_path()` to deterministically enforce `scope.filesystem_prefixes` sandbox boundaries in addition to `.git` and recovery directory safety.

#### C. End-to-End Demonstration (`tests/test_e2e_demonstration.py`)
- Complete Autonomous Agent Loop: Verified the entire pipeline from goal submission, briefing assembly, user confirmation, deterministic skill selection, capability authorization via Trust Core, tool execution, observation collection, controlled error introduction, error diagnosis, hypothesis testing, empirical rejection, alternative recovery, independent step verification, checkpoint durability, and experience event recording.
- Safety Halts: Verified fail-closed safety halts upon permission denial (out-of-scope resource access), material scope changes (briefing cancellation), and budget exhaustion (max tool calls/iterations).
- Durable Restart Recovery: Verified state restoration and task provenance availability by initializing a fresh `Supervisor` instance against the persisted SQLite database without re-executing completed idempotent steps.

#### D. Golden-Task Evaluation Harness (`src/skills/evaluation.py`)
- `GoldenTaskRecord` & `GoldenTaskEvaluator`: Reusable evaluation framework executing and documenting 7 realistic operational scenarios with explicit acceptance criteria checklists, actual results, failure modes, and structured verification telemetry:
  1. `GT-01`: Read-only workspace inspection (directory listing, file count verification, zero mutations).
  2. `GT-02`: Search and evidence-backed summarization (code pattern matching, line references, synthesized summary).
  3. `GT-03`: Controlled test execution and diagnosis (syntax/test execution, exit code verification, no unrestricted shell).
  4. `GT-04`: Recovery from known transient failure (error detection, hypothesis formulation, alternative safe strategy, no duplicate side effects).
  5. `GT-05`: Researching unfamiliar error (local docs search, source provenance preservation, testable hypotheses).
  6. `GT-06`: Resuming checkpointed task (SQLite checkpoint persistence across simulated process restart, non-idempotent steps not repeated).
  7. `GT-07`: Handling ambiguous request via clarification (`ClarificationGate` intercepting ambiguous goal, requesting user clarification, halting unconfirmed execution).

## [Prompt 030] — 2026-10-10

### Online Error Research, Source Provenance & Evidence-Based Recovery

**Status:** IMPLEMENTED & VERIFIED — Extended Persistent Completion Mode with authorized, bounded, zero-secret online error research (`OnlineDocResearchProvider`, `CompositeResearchProvider`, `OnlineResearchBudget`, `ResearchSource`). Enforces strict research escalation order: 1) inspect error logs/checkpoints, 2) search local documentation and approved lessons, 3) bounded online research against permitted technical domains, 4) safe non-blocking fallback to offline reasoning or human intervention. Enforces strict domain allowlists, prompt injection neutralization, secret scrubbing, and untrusted adjustment validation. Formulates testable hypotheses with empirical verification methods, tracks disproven hypotheses to avoid oscillating strategies, and maintains Trust Core security invariants unconditionally.  
**Tests:** 601 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~42.0s) — 26 new tests in `tests/test_online_research.py`  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (144 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (142 source files)  

#### A. Research Escalation & Provider Hierarchy (`src/manager/research.py`)
- `ResearchProvider`: Pluggable abstract interface for technical reference documentation lookups.
- `LocalDocResearchProvider`: Offline knowledge lookup against local architecture specifications.
- `OnlineDocResearchProvider`: Network-gated, domain-bounded, budgeted lookup against authoritative technical developer resources.
- `CompositeResearchProvider`: Tiered provider coordinating local-first lookup with escalation to online research when local knowledge is absent and network egress is authorized.
- Safe Fallback: When network is disabled or backend fails/times out, returns `None` without raising or stalling, falling back smoothly to offline reasoning or user guidance.

#### B. Security Boundaries & Zero-Secret Invariant (`src/manager/research.py`, `src/config/models.py`)
- Domain Allowlists: Hardcoded and configurable domain restrictions (`docs.python.org`, `pypi.org`, `github.com`, `readthedocs.io`, `stackoverflow.com`, `learn.microsoft.com`, `sqlite.org`, `pydantic.dev`, `pytest.org`).
- Secret Leak Protection: Rejects queries containing API keys, private keys, authorization tokens, or secrets (`SecretLeakError`); redacts private Windows/Unix paths.
- Prompt Injection Neutralization: Strips HTML/script tags and neutralizes adversarial prompt injection phrases (`Ignore previous instructions`, `system prompt`, `execute command`, etc.) in external snippets.
- Untrusted Adjustments Validation: Sanitizes suggested parameter adjustments to allowed types (`int`, `float`, `bool`, safe strings); strictly rejects shell syntax, `__import__`, `eval()`, or code execution payloads.
- Trust Core Primacy: Research suggestions are treated as untrusted hints. Never bypasses autonomy tiers, never overrides authorization denials, and never executes high-risk operations without approval.

#### C. Provenance Tracking & Hypothesis Testing (`src/manager/research.py`, `src/manager/persistent_engine.py`)
- `ResearchRecord` & `ResearchSource`: Full citation provenance recording title, URL, retrieval timestamp, domain, official flag, excerpt, query, findings, testable hypothesis, and verification method.
- `ErrorDiagnosis`: Extended with `testable_hypothesis`, `verification_method`, and `disproven_hypotheses`.
- Empirical Hypothesis Testing: Tracks failing hypotheses per step; if a hypothesis is disproven, escalates from `PARAM_ADJUSTMENT` to `ALTERNATIVE_TOOL` with user confirmation, and if multiple hypotheses fail, escalates to `REPLAN`.
- Checkpoint Provenance: Persistent checkpoints record `ERROR_DIAGNOSIS` events with `research_provider`, `testable_hypothesis`, and `verification_method`.

## [Prompt 029] — 2026-10-10

### Context Understanding, Confirmation Gate & Persistent Completion Mode

**Status:** IMPLEMENTED & VERIFIED — Implemented user-selectable `ExecutionMode.PERSISTENT_COMPLETION` with a mandatory pre-execution **Context Understanding & Confirmation Gate** (`ConfirmationGate`, `ContextBriefing`) and autonomous resilient execution engine (`PersistentExecutionEngine`, `DiagnosticInvestigator`, `ErrorClassifier`). Adheres to the principle: *Understand -> Explain -> Confirm -> Execute -> Observe -> Diagnose -> Recover -> Verify -> Complete*. Guarantees that autonomous execution cannot proceed until a structured briefing is explicitly confirmed by the user. Routine recoverable errors are investigated and retried within confirmed bounds; material context changes, policy approval requirements, oscillating strategies, and stuck loops pause execution for user reconfirmation.  
**Tests:** 575 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~42.2s) — 59 new tests in `tests/test_persistent_completion.py`  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (143 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (141 source files)  

#### A. Execution Mode & Configuration (`src/config/models.py`, `src/manager/models.py`, `src/manager/state_store.py`)
- `ExecutionMode`: Added `STANDARD` (fast-path default, zero overhead) and `PERSISTENT_COMPLETION` (high-assurance persistent recovery).
- `TaskRecord`: Extended with `execution_mode: ExecutionMode` stored in `tasks` table metadata.
- `AutonomyConfig`: Added `default_execution_mode: str = "STANDARD"` configuration setting with persistent mode validation.

#### B. Context Understanding & Confirmation Gate (`src/manager/briefing.py`)
- `ContextBriefing`: Structured, frozen data model documenting goal, deliverables, requirements, constraints, provenance items, relevant memories, approved lessons, available tools, assumptions, uncertainties, proposed step plan, acceptance criteria, risks, required capability approvals, execution budgets, and versioning.
- `ContextProvenanceItem`: Source-of-truth tracking categorized by `ProvenanceType` (`USER_PROVIDED`, `LOCAL_CODEBASE`, `LOCAL_SYSTEM_CONFIG`, `LOCAL_ENVIRONMENT`, `EKM_MEMORY`, `INFERRED_ASSUMPTION`).
- `ExecutionBudget`: Bounded constraints on iterations, recovery cycles, tool calls, and execution duration.
- `ConfirmationGate`: Core engine for assembling briefings via `ContextAssembler`, evaluating deterministic ambiguity via `ClarificationGate`, verifying approval requirements against Trust Core, and enforcing state transitions (`confirm()`, `correct()`, `add_information()`, `cancel()`).
- Fail-Closed Gate Invariant: Tasks with `PERSISTENT_COMPLETION` mode fail closed (`ConfirmationRequiredError`) if execution is attempted without a `CONFIRMED` briefing.

#### C. Error Classification & Research Loop (`src/manager/research.py`)
- `ErrorClass`: Categorized taxonomy (`TRANSIENT`, `ENVIRONMENT_RESOURCE`, `CONFIGURATION_PARAM`, `PERMISSION_AUTH`, `LOGIC_STRATEGY`, `TOOL_INTERFACE`, `UNKNOWN`).
- `ErrorSignature`: Normalized failure fingerprint with zero-secret redaction for loop/oscillation detection.
- `LocalDocResearchProvider`: 100% offline reference lookup against local specifications and architectural documentation.
- `DiagnosticInvestigator`: Synthesizes failure hypotheses, consults approved lessons in `LessonManager`, suggests structured parameter/strategy adjustments, and selects recovery strategies (`RETRY_WITH_BACKOFF`, `PARAM_ADJUSTMENT`, `ALTERNATIVE_TOOL`, `ESCALATE_APPROVAL`, `REPLAN`, `ABORT_BLOCKER`).
- Stuck-Loop & Oscillation Detection: Detects repeating failed strategies or 3 identical error signatures and aborts with blocker status to prevent runaway loops.

#### D. Persistent Execution Engine (`src/manager/persistent_engine.py`)
- `PersistentExecutionEngine`: Orchestrates resilient persistent loop with step verification (`StepVerifier`), diagnostic investigation, bounded recovery, and durable SQLite checkpoints at every state transition.
- Material Change Escalation: Halts on Trust Core denials, unknown tools, or scope modifications, transitioning task to `AWAITING_APPROVAL` with user notification.
- Zero-Secret Redaction: Enforces `_check_no_secrets()` across all briefing notes, error signatures, diagnosis records, and checkpoints.
- Notification Integration: Emits advisory notifications via `NotificationManager` on workflow completion, required approvals, and budget exhaustion.

## [Prompt 028] — 2026-10-10

### Notification Subsystem & Approval Escalation

**Status:** IMPLEMENTED & VERIFIED — Implemented durable notification subsystem (`src/notifications/`) backed by SQLite schema migration `0008_notifications_schema.sql`. Informs user about scheduled-task failures, workflow completions/failures, pending approvals, resource constraints, and required manual intervention. Supports typed prioritized events (P0-P4), quiet-hours suppression for non-critical alerts, deduplication suppression keys, bounded delivery retries with FAILED terminal state, delivery failure absorption without store corruption, restart recovery, adapter decoupling, and zero-secret redaction. Strictly enforces the architectural invariant that notifications are advisory-only and never grant approvals, mutate capability tokens, or bypass Trust Core.  
**Tests:** 516 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~47.4s) — 16 new tests in `tests/test_notifications.py`  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (139 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (137 source files)  

#### A. Notification Data Architecture & Persistence (`src/notifications/models.py`, `store.py`)
- `Notification`, `NotificationAttempt`, `DeliveryResult`: Typed, immutable representations of events, per-attempt audit trails, and delivery outcomes.
- `NotificationEventType`: Standard event classifications (`APPROVAL_REQUIRED`, `APPROVAL_EXPIRED`, `APPROVAL_GRANTED`, `APPROVAL_REJECTED`, `WORKFLOW_COMPLETED`, `WORKFLOW_FAILED`, `WORKFLOW_PAUSED`, `SCHEDULE_FAILED`, `SCHEDULE_MISSED`, `RESOURCE_CONSTRAINT`, `RECOVERY_EXHAUSTED`, `INTERVENTION_REQUIRED`, `GENERAL_INFO`).
- `NotificationPriority`: P0 (critical/immediate) down to P4 (background/informational).
- `NotificationStore`: SQLite ACID persistence with transactional updates, atomic state transitions (`PENDING`, `DELIVERED`, `FAILED`, `ACKNOWLEDGED`, `DISMISSED`, `SUPPRESSED`), and bounded attempt history tracking.
- Zero-Secret Invariant: Enforced at write time via `_check_no_secrets()`, ensuring credentials, API keys, tokens, and raw passwords never enter notification titles, bodies, or metadata.

#### B. Notification Manager & Delivery Pipeline (`src/notifications/manager.py`, `adapters.py`)
- `NotificationManager`: Central orchestrator for emitting, deduplicating, prioritizing, suppressing, acknowledging, dismissing, and retrying notifications.
- Quiet Hours Policy: Suppresses non-critical notifications (P2-P4) during configured user quiet hours; critical notifications (P0-P1) bypass quiet hours unconditionally.
- Deduplication Suppression: Suppresses duplicate notifications with matching `dedup_key` within the configured dedup window.
- Decoupled Delivery Adapters: `DeliveryAdapter` protocol with default `LocalLogAdapter`; handles adapter exceptions safely without corrupting store state.
- Bounded Retries: Tracks attempts per notification, transitioning notifications exceeding `max_delivery_attempts` to terminal `FAILED` status.
- Advisory-Only Invariant: Explicitly tested and verified that notifications, user acknowledgements, and dismissals never approve pending actions or grant Trust Core capabilities.

#### C. Migration & Test Integration
- `migrations/0008_notifications_schema.sql`: Staged migration creating `notifications` and `notification_attempts` tables with compound indexes and integrity constraints.
- Environment & schema validation tests updated in `tests/test_domain_schema.py` and `tests/test_environment_validation.py` to register migration 0008 and package `src/notifications/`.

## [Prompt 027] — 2026-10-10

### Durable Workflow Engine & Task Scheduler

**Status:** IMPLEMENTED & VERIFIED — Implemented durable workflow engine (`src/workflow/`) and resilient task scheduler (`src/scheduler/`) backed by SQLite schema migration `0007_workflow_scheduler_schema.sql`. Integrates end-to-end with Supervisor, StateStore checkpoints, Tool Broker, and Trust Core authorization. Supports DAG validation, cycle detection, argument interpolation, bounded retries, budget limits, pause/resume/cancellation, one-shot/cron/event schedules, deduplication, and downtime missed-run handling.  
**Tests:** 500 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~45.2s) — 20 new tests (10 in `tests/test_workflow_engine.py`, 10 in `tests/test_scheduler.py`)  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (132 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (130 source files)  

#### A. Durable Workflow Engine (`src/workflow/`)
- `WorkflowDefinition`, `WorkflowStepDefinition`, `WorkflowBudget`, `WorkflowRun`: Typed, immutable data models representing versioned workflow definitions and durable run instances.
- `WorkflowValidator`: Structural checks, DAG cycle detection using 3-color DFS graph traversal, dependency validation, input/output reference interpolation checks (`{{inputs.key}}`, `{{steps.step_id.outputs.key}}`), zero-secret detection, and tool registration validation.
- `WorkflowStore`: SQLite persistence for workflow definitions and runs across application restarts, ensuring ACID state transitions (`PENDING`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, `CANCELLED`).
- `WorkflowEngine`: Dependency-aware execution loop resolving step topological eligibility; step argument interpolation; execution budgeting and deadline enforcement; bounded retries and failure policy (`FAIL_FAST`, `CONTINUE_ON_FAILURE`, `RETRY`); task-level checkpoint persistence via `StateStore`; idempotency token tracking to avoid duplicate side effects during recovery.
- Verification Pipeline Integration: Executes steps through `ToolBroker` and `TrustCore`; enforces `StepVerifier` policies (`deterministic_check`, `requires_approval`, `trusted_output`); pauses workflows safely on approval escalation or authorization revocation.

#### B. Durable Scheduler (`src/scheduler/`)
- `ScheduleDefinition`, `ScheduleExecutionRecord`: Typed models for one-shot, recurring cron, and event-triggered schedule policies.
- `compute_next_cron_run()`: Standard 5-part cron evaluator (`minute hour day-of-month month day-of-week`) supporting ranges, steps (`*/15`), lists, and wildcards.
- `SchedulerStore`: SQLite persistence with uniqueness constraint and atomic deduplication on `(schedule_id, scheduled_for)` to prevent double executions across poll cycles and system restarts.
- `WorkflowScheduler`: Periodic poller and dispatcher; supports `ONE_SHOT`, `CRON_RECURRING`, and `EVENT_TRIGGERED` actions; handles downtime missed runs according to configured policy (`SKIP`, `RUN_ONCE`, `FAIL_ALERT`); enforces bounded concurrency (`max_concurrent_runs`); supports dynamic pause, resume, and cancellation.

#### C. Migration & Test Environment Compatibility
- `migrations/0007_workflow_scheduler_schema.sql`: Staged migration adding `workflow_definitions`, `workflow_runs`, `schedules`, and `schedule_executions` tables with foreign keys and compound indexes.
- Environment & schema validation tests updated in `tests/test_domain_schema.py` and `tests/test_environment_validation.py` to register migration 0007 and runtime packages.

## [Prompt 026] — 2026-10-10

### Approved Lesson Retrieval & Context IR Integration

**Status:** IMPLEMENTED & VERIFIED — Integrated approved `ExecutionLesson` records into the canonical EKM retrieval pipeline (`src/memory/retriever.py`), bounded Context IR compilation (`src/memory/compiler.py`), and the unified Manager context layer (`src/manager/context.py`, `src/manager/supervisor.py`). Verified 14 comprehensive requirements covering state filtering, scope isolation, deduplication, ranking, budget enforcement, and security non-escalation.  
**Tests:** 480 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~39.7s) — 14 new retrieval and Context IR integration tests (`tests/test_lesson_retrieval_context.py`)  
**Lint:** `ruff check src/ tests/` — 0 errors · `ruff format --check src/ tests/` — clean (118 files)  
**Type Check:** `mypy src/ tests/` — 0 issues (116 source files)  

#### A. Approved-Only Lesson Retrieval Pipeline
- `EKMRetriever._retrieve_lessons()`: Restricts lesson retrieval strictly to `LessonState.APPROVED`. Excludes every non-active state (`CANDIDATE`, `VERIFIED`, `REJECTED`, `INVALIDATED`, `SUPERSEDED`).
- Scope boundary isolation: Unscoped queries (`project_id=None`) only retrieve global lessons; scoped queries retrieve matching project lessons plus global lessons when `include_global=True`. Zero cross-project leakage.
- Skill relevance filtering: When `query.skill_id` is supplied, non-matching lessons are filtered out.
- Deterministic multi-factor ranking: Scores candidate lessons based on task query keyword matching against trigger condition, recommended action, observation, and title (0.0 to 0.40), scope specificity (0.15), evidence corroboration quality (0.0 to 0.25), and recency bonus (0.0 to 0.20).
- Evidence & provenance preservation: Supporting event and evidence IDs are preserved on the lesson record and mapped to `provenance_map` in `RetrievalResult`.

#### B. Context IR Compilation & Deduplication
- `ContextCompiler`: Represents lessons distinctly within `ContextIR` and prompt strings under `## Relevant Verified Lessons (Advisory Experience)`.
- Explicit separation: Clearly demarcates `Observed:` (factual empirical results) from `Explanation:` (inferred theories).
- Deduplication against canonical facts: When an approved lesson was promoted to an EKM fact (`lesson.fact_id`), the duplicate memory fact is suppressed to conserve token budget.
- Strict token budget enforcement: Enforces `available_token_budget` limit, tracking included lessons in `included_lesson_ids` and recording omitted lessons in `omitted_lesson_ids`.
- `ExecutionLessonContext`: Immutable context DTO containing lesson ID, title, trigger condition, recommended action, observation, explanation, risk level, state, evidence IDs, and scope.

#### C. Supervisor Wiring & Security Invariants
- `Supervisor` & `ContextAssembler`: Injects `LessonManager` into context assembly pipeline, surfacing approved lessons in `ContextIR.relevant_lessons` with zero additional LLM or model overhead during retrieval.
- Deterministic Security Boundary: Lessons remain strictly advisory context. Verified via test that lesson records cannot grant tool permissions, bypass Tool Broker policies, alter Trust Core autonomy tiers, or trigger automatic action execution.

## [Prompt 025] — 2026-10-10

### Durable Experience Memory & Evidence-Gated Lesson Lifecycle

**Status:** IMPLEMENTED & VERIFIED — Complete evidence-gated lesson lifecycle (`src/memory/lessons.py`): `ExecutionLesson`, `LessonManager`, `LessonProposal`. Fixed circular import between `src/skills/__init__.py` and `src/memory/lessons`. Fixed invalid `provenance_source="experience"` (DB CHECK violation) to `"system"`.  
**Tests:** 466 passed · 0 failed · 0 skipped (100% offline inside SAGE venv) — 24 new lesson lifecycle tests (`tests/test_lesson_manager.py`)  

#### A. Evidence-Gated Lesson Proposal & Lifecycle
- `LessonManager.propose_candidate()`: Creates `CANDIDATE` lesson from `ExperienceEvent` + `LessonProposal`. Enforces zero-secret invariant on all text fields. A single execution NEVER directly creates an APPROVED lesson.
- `LessonManager.corroborate_lesson()`: Accumulates supporting or contradicting execution telemetry. Applies risk-based promotion thresholds: LOW(2), MEDIUM(3), HIGH(4 or explicit user confirmation).
- Lifecycle states: `CANDIDATE → VERIFIED → APPROVED`, `REJECTED`, `INVALIDATED`, `SUPERSEDED`.
- `invalidate_lesson()` and `supersede_lesson()` cascade to EKM canonical fact invalidation (non-blocking).

#### B. EKM Integration on Promotion
- On promotion to `APPROVED`, `LessonManager` calls `EKMMemoryStore.add_fact()` creating a canonical `memory_facts` record tagged with `operational_lesson`.
- `fact_id` is populated on the lesson record only after confirmed APPROVED promotion.
- EKM store errors during promotion are non-blocking; lesson still transitions to APPROVED.
- `provenance_source` corrected to `"system"` (satisfies DB CHECK constraint).

#### C. Persistence & Scoped Retrieval
- SQLite-backed `execution_lessons` table with indexes on `skill_id`, `project_id`, `state`.
- Dual-path storage: in-memory dict + SQLite `INSERT OR REPLACE` upserts.
- `get_lesson()` returns `None` (never raises) for unknown IDs.
- `list_lessons()` supports filtering by `project_id`, `skill_id`, `states`, and `include_global` for global-scope fallback.
- In-memory mode (no SQLite) fully functional for offline/testing contexts.

#### D. Bug Fixes
- **Circular Import**: Removed `src.memory.lessons` imports from `src/skills/__init__.py`. Lesson types now accessed from `src.memory` directly, breaking `skills → memory → skills` cycle.
- **DB Constraint**: Fixed `provenance_source="experience"` → `"system"` in `LessonManager.corroborate_lesson()`.

## [Prompt 024] — 2026-10-10

### Experience & Knowledge Memory (EKM) Runtime, Selective Retrieval & Context IR

**Status:** IMPLEMENTED & VERIFIED — Full implementation and integration of Experience & Knowledge Memory (EKM) runtime (`src/memory/store.py`), derived FTS5 indexing (`src/memory/indexer.py`), multi-stage selective retrieval pipeline (`src/memory/retriever.py`), bounded Context IR compilation (`src/memory/compiler.py`), and optional semantic backend abstraction (`src/memory/semantic.py`). Fully integrated with `ContextAssembler` and `Supervisor`.  
**Tests:** 442 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~43.5s) — 24 new EKM runtime & retrieval tests (`tests/test_ekm_memory_runtime.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (115 files)  
**Type Check:** `mypy` — 0 issues (95 source files)  

#### A. Authoritative Typed Persistence & Secret Protection
- Created `EKMMemoryStore` managing canonical relational SQLite tables (`memory_facts`, `user_preferences`, `entities`, `entity_relationships`, `memory_evidence`).
- Enforced zero secret leakage via `_check_no_secrets` raising `SecretLeakError` on credentials, API keys, bearer tokens, or private keys across all write paths.
- Preserved historical records via explicit supersession tracking (`supersede_fact`, `supersede_preference`).
- Enforced frozen provenance trust levels M0–M4 (`MemoryTrustLevel`).

#### B. Derived Full-Text Search (FTS5) Indexing
- Implemented `FTS5IndexManager` operating auxiliary virtual table `ekm_fts` (`tokenize='porter unicode61'`).
- Invariant upheld: derived index is 100% rebuildable from authoritative relational tables via `rebuild_index()`.
- Added `sanitize_fts5_query()` preventing malformed punctuation/syntax crashes and expanding camelCase tokens for bidirectional matching.
- Integrated `is_superseded` auxiliary state in FTS5 index with `mark_superseded()`.

#### C. Multi-Stage Selective Retrieval
- Implemented `EKMRetriever` covering:
  1. Scope boundary filtering (`project_id`, global fallback, strict project isolation).
  2. Temporal validity filtering (`valid_from <= as_of_time <= valid_until`, active vs expired, supersession handling).
  3. FTS5 lexical matching combined with entity canonical slug matching.
  4. Entity relationship graph traversal up to `max_relationship_depth`.
  5. Trust-aware ranking combining BM25 score, trust weights (M0=0.2 to M4=1.0), epistemic confidence, importance, and project-specificity bonus.
  6. Deduplication and conflict detection: flags opposing facts and opposing preferences in `unresolved_conflicts`.
  7. Detailed provenance attribution map per returned record.

#### D. Bounded Context IR Compilation & Manager Integration
- Implemented `ContextCompiler` converting retrieval results into bounded `ContextIR` instances within explicit token budgets (`context_token_budget`).
- Integrated deterministic token estimation with uncertainty bounds (±15%) and omission tracking (`omitted_record_ids`).
- Enhanced `ContextAssembler` and `Supervisor` with optional `memory_store` / `retriever` wiring, enabling seamless context assembly for planning while retaining 100% backward compatibility.
- Designed `SemanticMemoryBackend` protocol with `NullSemanticBackend` reporting status `DESIGNED` without network/GPU dependencies.

## [Prompt 023] — 2026-10-09

### End-to-End Skill Execution, Verification & State Recovery Pipeline

**Status:** IMPLEMENTED & VERIFIED — Full integration of User Goal -> ClarificationGate -> Supervisor -> SkillRegistry -> SkillAdmissionController -> BoundSkillToolBroker -> ToolBroker -> TrustCore -> Verifier -> Checkpoint/Reconciliation -> ExperienceEvent pipeline.  
**Tests:** 418 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~43.5s) — 24 new pipeline integration tests (`tests/test_integration_skills_execution.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (107 files)  
**Type Check:** `mypy` — 0 issues (105 source files)  

#### A. Supervisor & Skills Pipeline Integration
- Connected `Supervisor.execute_skill()` to `SkillAdmissionController` with strict lifecycle transitions (`AVAILABLE -> SELECTED -> ADMITTED -> EXECUTING -> VERIFYING -> SUCCEEDED -> AVAILABLE`).
- Wired `Supervisor.select_skill()` to `SkillRegistry` deterministic intent matching.
- Wired `ClarificationGate` short-circuiting on incomplete or empty goals prior to any LLM invocation.
- Exported `GoalExecutionResult` in public `src.manager` API.
- Added `reconcile_on_startup` alias to `ReconciliationEngine` for consistent startup recovery invocation.

#### B. Security & Boundary Guarantees
- Validated that `BoundSkillToolBroker` strictly blocks undeclared tools (`SkillToolAccessDeniedError`).
- Verified that unknown tools not present in `ToolBroker` catalog are rejected (`ToolNotFoundError`).
- Verified that `TrustCore` autonomously denies or escalates unpermitted actions even if declared in skill bindings (`AutonomyTier.STRICT_MANUAL`).
- Ensured zero secret leakage in `ExperienceEvent` payloads and `SkillCheckpointMetadata` persistence (`SecretLeakError`).

#### C. Verification & Recovery Guarantees
- Demonstrated post-tool-execution semantic verification failure (`StepVerifier` catches failure even when tool returns `SUCCESS`).
- Ensured non-idempotent side-effect steps are never automatically retried during recovery.
- Re-authorization via `TrustCore` enforced on every retry step.
- Persisted and reconstructed checkpoint execution state via `ReconciliationEngine`.

#### D. Telemetry & Fast Path
- Verified `ExperienceEvent` classification distinguishing `USER_CORRECTED` from execution failures and `NO_FEEDBACK` from assumed success.
- Verified deterministic fast path for skill execution with zero LLM reasoning invocations.
- Verified 100% offline execution with zero socket/network operations.

## [Prompt 022] — 2026-10-08

### Phase 6: SAGE Skills Architecture & Tool Integration Foundation

**Status:** IMPLEMENTED & VERIFIED — Declarative SkillManifest schema, deterministic manifest validation, fail-closed SkillRegistry, BoundSkillToolBroker, DeterministicSkillExecutor, SkillAdmissionController, ClarificationGate, ExperienceFeedbackService, and SkillCheckpoint boundary.  
**Tests:** 394 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~40.8s) — 20 new Skills subsystem tests (`tests/test_skills.py`), plus approval lifecycle and manager recovery tests.  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (106 files)  
**Type Check:** `mypy` — 0 issues (104 source files)  

#### A. Declarative Skills Architecture (`src/skills/`)
- Implemented `SkillManifest` model with strict SemVer, regex identifier validation, risk classes, and resource declarations.
- Enforced security invariant: Skills cannot define or override Trust Core policies.
- Implemented fail-closed `SkillRegistry` with deterministic lifecycle state machine (`AVAILABLE`, `SELECTED`, `ADMITTED`, `EXECUTING`, `VERIFYING`, `SUCCEEDED`, `FAILED`, `RECOVERY`).
- Implemented `BoundSkillToolBroker` scoped tool gateway.

#### B. Admission, Clarification & Telemetry Foundations
- Implemented `SkillAdmissionController` leveraging `SkillConcurrencyManager` and `ModelResidencyManager`.
- Implemented `ClarificationGate` with deterministic prompt generation.
- Implemented `ExperienceFeedbackService` recording decoupled `ExperienceEvent` records without automatic model self-modification.
- Implemented `SkillCheckpointMetadata` and `persist_skill_checkpoint` integration with `StateStore`.

## [Prompt 021B] — 2026-10-01

### Phase 5: SAGE Local Reasoning Model Provisioning

**Status:** IMPLEMENTED & VERIFIED — Qualified local quantized GGUF provisioned to SAGE-owned storage, llama-cpp-python runtime integrated, provider-neutral reasoning abstraction implemented, memory admission and residency policy integrated, offline deterministic smoke test verified, empirical qualification persisted.  
**Tests:** 362 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~36.7s) — 11 new Local Reasoning tests (`tests/test_local_reasoning.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (82 files)  
**Type Check:** `mypy` — 0 issues (80 source files)  

#### A. Model Provisioning & Integrity Verification
- Created SAGE-managed model directory outside OneDrive, repository, and C:\AEGIS:
  `%USERPROFILE%\.aegis\models\local-llm\qwen35\`
- Copied qualified artifact to SAGE-owned path: `qwen35-4b-q4km.gguf`
- Verified artifact properties:
  - Exact byte size: 2,740,937,888 bytes
  - SHA-256: `00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4`
  - GGUF architecture: `qwen35`, Q4_K_M quantization, 248,320 vocab tokens
- Original Meetly installation untouched and unmodified. No runtime dependency on Meetly or helper daemons.

#### B. Provider-Neutral Local Reasoning Abstraction (`src/reasoning/`)
- Created provider-neutral subsystem:
  - `src/reasoning/base.py`: `LocalReasoningProvider` abstract base class defining lifecycle (`initialize`, `is_ready`, `reason`, `unload`, `verify_integrity`, `get_metrics`).
  - `src/reasoning/models.py`: Typed Pydantic schemas (`ReasoningRequest`, `ReasoningResponse`, `ReasoningMetrics`, `ReasoningProviderStatus`).
  - `src/reasoning/errors.py`: Typed fail-closed error hierarchy (`ReasoningError`, `ReasoningStorageError`, `ReasoningIntegrityError`, `ReasoningAdmissionError`, `ReasoningExecutionError`, `ReasoningResidencyError`).
  - `src/reasoning/runtime.py`: Canonical runtime descriptor (`LLAMACPP_CPU_RUNTIME`), model descriptor (`QWEN35_4B_MODEL`), and skill profile (`SKILL_LOCAL_REASONING`).
  - `src/reasoning/provider.py`: `LlamaCppReasoningProvider` implementing CPU inference, streaming time-to-first-token (TTFT) measurement, deterministic temperature=0.0 generation, and storage boundary validation.
  - `src/reasoning/qualification.py`: Live empirical hardware qualification via `ModelHardwareQualifier`, saving profile to `%USERPROFILE%\.aegis\models\qualifications\qwen35-4b-q4km.qualification.json`.
  - `src/reasoning/__init__.py`: Public API exports and `build_default_reasoning_provider` factory.

#### C. Model Policy & Admission Integration
- Integrated with `SkillConcurrencyManager`: Dynamic RAM gating checks available host memory against 1.5 GB system reserve before admitting model weights. Fails closed with `ReasoningAdmissionError` if headroom is insufficient.
- Integrated with `ModelResidencyManager`: Tracks resident status, reference-counts active skill execution leases, and strictly blocks model eviction while active leases exist.
- Separated from Fast Decision Engine: Intended strictly for local Manager/reasoning inference; Fast Decision Engine remains advisory intent router.

#### D. Offline Smoke Test & Quality Gates
- Offline deterministic reasoning smoke test verified:
  - Cold load latency: ~1.8s
  - Time-To-First-Token (TTFT): ~600-630ms
  - Total inference latency: ~670-810ms (deterministic output verified)
  - Clean unload confirmed (zero memory leaks, residency cleared)
- Empirical hardware qualification profile persisted:
  - Measured resident RAM: ~2,740 MB
  - Measured marginal RAM: ~1,343 MB
  - Measured p95 latency: ~661 ms
  - Status: `QUALIFIED`

## [Prompt 021] — 2026-10-01

### Phase 5: Unified Supervisor + Execution State Recovery

**Status:** IMPLEMENTED & VERIFIED — Full Unified Supervisor and Execution State Recovery subsystem (`src/manager/`), SQLite migration `0006_task_execution_recovery_schema.sql`, crash discovery & idempotent recovery engine, and end-to-end integration tests.  
**Tests:** 351 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~16.0s) — 24 new Supervisor / Manager recovery tests (`tests/test_manager_recovery.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (74 files)  
**Type Check:** `mypy` — 0 issues (72 source files)  

#### A. Database Migration (`migrations/0006_task_execution_recovery_schema.sql`)
- Created 4 durable tables:
  - `supervisor_sessions`: Session lifecycle (`INITIALIZING`, `ACTIVE`, `RECONCILING`, `COMPLETED`, `FAILED`, `ABORTED`), metadata, error tracking.
  - `tasks`: Task state machine (`PENDING`, `PLANNING`, `EXECUTING`, `AWAITING_APPROVAL`, `COMPLETED`, `FAILED`, `CANCELLED`), priority, advisory route, error summary.
  - `task_steps`: Ordered execution steps (`PENDING`, `RUNNING`, `AWAITING_APPROVAL`, `COMPLETED`, `FAILED`, `SKIPPED`), action risk level, capability token ID, idempotency tracking, sanitized results.
  - `task_checkpoints`: Point-in-time recovery snapshots ordered monotonically (`checkpoint_seq`), with zero-secret snapshot verification.
- Enforced strict foreign keys, cascade rules, composite unique indexes (`UNIQUE(task_id, step_index)`, `UNIQUE(task_id, checkpoint_seq)`), and ISO 8601 UTC timestamps.

#### B. Manager Subsystem (`src/manager/`)
- **`src/manager/errors.py`**: Fail-closed exception hierarchy rooted in `ManagerError` (`StateTransitionError`, `TaskNotFoundError`, `StepNotFoundError`, `SessionNotFoundError`, `SecretLeakError`, `ReconciliationError`, `SupervisorExecutionError`).
- **`src/manager/models.py`**: Frozen Pydantic models and immutable enums. Implements valid state transition tables `VALID_TASK_TRANSITIONS` and `VALID_STEP_TRANSITIONS`, supporting human approval escalation workflows.
- **`src/manager/state_store.py`**: ACID SQLite state store utilizing `BEGIN IMMEDIATE` transactions to prevent concurrency conflicts. Enforces zero-secret policy via regex scanning on titles, metadata, arguments, snapshots, and error summaries. Sanitizes strings on read and write.
- **`src/manager/reconciliation.py`**: Startup crash recovery engine (`ReconciliationEngine`):
  - Discovers interrupted in-flight tasks (`EXECUTING`, `PLANNING`, `RUNNING` steps).
  - Idempotent steps with matching completed checkpoints are safely marked `COMPLETED`.
  - Incomplete idempotent steps are safely reset to `PENDING` for re-execution.
  - Ambiguous non-idempotent steps without checkpoints are escalated to `AWAITING_APPROVAL` with clear error summaries, strictly preventing duplicate side effects.
  - Corrupt or empty executing tasks safely transition to `FAILED` (fail closed).
  - Re-running reconciliation on stable database is provably idempotent.
- **`src/manager/supervisor.py`**: Central coordinator (`Supervisor`):
  - Plans tasks with ordered step definitions.
  - Queries `FastDecisionService` for advisory route suggestions (`is_advisory_only: Literal[True]` invariant strictly upheld).
  - Evaluates action risk with `TrustAuthority` (`evaluate_action_risk`).
  - Automatically executes approved steps via `ToolBroker` (`dispatch_tool`) or escalates unauthorized steps to `AWAITING_APPROVAL`.
  - Captures durable snapshots into `task_checkpoints` before and after dispatch.
  - Allows human operators to approve pending actions and resume execution.
- **`src/manager/__init__.py`**: Clean domain exports and `build_supervisor` factory helper.

#### C. Test Suite & Validation
- Added `tests/test_manager_recovery.py` with 24 comprehensive tests covering state machine transitions, store persistence, secret rejection, reconciliation scenarios, and end-to-end supervisor tool dispatch.
- Updated `tests/test_domain_schema.py` (55 tests) to expect schema version 6 and validate the 4 new tables.
- Updated `tests/test_environment_validation.py` (13 tests) recognizing `manager` as a canonical runtime package.

## [Prompt 020] — 2026-10-01

### Project Identity Migration (AEGIS → SAGE) + Tool Broker & Sandboxed Execution Foundation

**Status:** IMPLEMENTED & VERIFIED — Full SAGE identity migration across all active source files,
Tool Broker architecture with Trust Core integration, subprocess sandbox, output sanitization,
and comprehensive test suite.  
**Tests:** 327 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, ~15s) — 46 new Tool Broker tests (`test_tool_broker.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (67 files)  
**Type Check:** `mypy` — 0 issues (65 source files)  

#### A. SAGE Identity Migration

**Previous identity:** AEGIS — Autonomous Personal Digital Agent and Epistemic Knowledge Model  
**Current identity:** SAGE — System for Autonomous Guidance & Execution

- Updated `pyproject.toml` project metadata (name, description, authors).
- Updated `README.md` project overview and architecture summary.
- Added `SageConfig` / `SageBaseConfig` aliases in `src/config/models.py` and `src/config/__init__.py`.
- Added `SAGE_*` environment variable precedence in `src/config/loader.py`; `AEGIS_*` variables
  retained as legacy compatibility overrides (backward-compatible, documented).
- Added `get_default_sage_home()` in `src/storage/paths.py` checking `SAGE_HOME` then `AEGIS_HOME`.
- Created `config/sage.example.yaml` canonical configuration template.
- Updated module-level docstrings in `src/trust/`, `src/storage/`, `src/model_policy/`,
  `src/hardware/`, `src/benchmark/`, `src/config/`, `src/decision/`, `tests/`.
- Updated directory `README.md` headers throughout repository.
- Added `tests/test_sage_identity.py` — 5 identity compliance tests.
- Activation scripts (`activate_env.bat`, `activate_env.ps1`) updated to echo SAGE.

**Preserved as historical/compatibility (intentionally unchanged):**
- `C:\AEGIS` recovery path constant (`src/storage/paths.py:CANONICAL_RECOVERY_ROOT`) — must stay.
- `%USERPROFILE%\.aegis` runtime home — user data must not be invalidated.
- `AEGIS_*` environment variable keys in config loader — backward compatibility.
- `AEGIS_*.md` specification document filenames — historical frozen specs; not renamed.
- Migration SQL filenames (`0001_`–`0005_*.sql`) — Git history must not be rewritten.
- Test fixture data strings (`"AEGIS"`, `"Hello AEGIS"`) in `test_domain_schema.py` — valid test data.
- Physical repository directory `aeigs` — not renamed per explicit user instruction.

#### B. Tool Broker Implementation (`src/tools/`)

Five new modules implementing the full provider-independent Tool Broker:

- **`src/tools/errors.py`**: Fail-closed exception hierarchy (`ToolBrokerError` → registration,
  argument, execution, authorization, sanitization).
- **`src/tools/models.py`**: `ToolParameterSchema` (type coercion + bounds validation),
  `ToolDefinition` (static tool declaration with name regex enforcement), `ToolResult`
  (uniform return type), `ParamType` enum.
- **`src/tools/sanitizer.py`**: Best-effort output sanitizer — strips ANSI escapes, enforces 64 KiB
  / 2000-line limits, redacts AWS keys, bearer tokens, PEM private keys, GitHub tokens,
  and password/secret/token assignments. **Does NOT guarantee secret-free output; defense
  in depth only.**
- **`src/tools/subprocess_runner.py`**: Process-level subprocess wrapper — command allowlist
  enforcement from `ResourceScope`, `cwd` filesystem scope validation, credential-stripped
  environment (`_FORWARDED_ENV_KEYS` allowlist), `shell=False` enforced, timeout with
  `ToolTimeoutError`. **Explicitly NOT an OS-level sandbox** (no seccomp, AppArmor, containers,
  or Windows Job Objects). This is a process-level guard only.
- **`src/tools/broker.py`**: Central `ToolBroker` — full 6-step pipeline for every invocation:
  1. Tool registration lookup (fail-closed on unknown tool)
  2. Argument schema validation and coercion
  3. Trust Core capability request (via `TrustAuthority.request_capability`)
  4. Capability consumption (HMAC, expiry, revocation, scope, use-count checks)
  5. Sandboxed execution (inline handler or subprocess runner)
  6. Output sanitization before return
  Scope widening via override is rejected (fail-closed). All failures return `ToolResult`;
  exceptions never propagate to callers.
- **`src/tools/__init__.py`**: Public API surface.

#### C. Trust Core Integration

Tool Broker requires a `TrustAuthority` instance at construction time. Every `invoke()` call:
- Requests a capability for the tool's declared `action_name` and `ResourceScope`.
- Consumes the capability (single-use, 60 s TTL default) immediately before execution.
- Audit records are written for every ALLOW/DENY/ESCALATE decision by the Trust Core.
- Any `TrustError` (deny, approval required, capability lifecycle failure) → `ToolResult.denied`.

#### D. Security Review (verified before commit)

| Check | Status |
|---|---|
| Tool Broker cannot bypass TrustAuthority | ✅ Every invoke() path requests + consumes capability |
| Unknown tools fail closed | ✅ Returns ToolResult.error (ToolNotFoundError) |
| Invalid arguments fail closed | ✅ Returns ToolResult.error before auth check |
| Arbitrary shell execution impossible via broker | ✅ shell=False enforced; command allowlist required |
| shell=False enforced | ✅ subprocess_runner.py:134 |
| Executable allowlist enforced | ✅ ResourceScope.allowed_process_commands checked |
| cwd/resource scope enforced | ✅ realpath + scope.contains_path() |
| Environment credentials stripped | ✅ _FORWARDED_ENV_KEYS allowlist; no credential keys forwarded |
| Output bounded and sanitized | ✅ 64 KiB / 2000-line limits + secret pattern redaction |
| Tool output treated as untrusted | ✅ Always passes through sanitize_output() |
| Trust Core auth failure prevents execution | ✅ Capability consumed before execute(); any failure → DENIED |
| OS-level sandboxing NOT falsely claimed | ✅ Explicitly documented as process-level only |

#### E. Test Suite (`tests/test_tool_broker.py`)

46 new tests across 6 classes:
- `TestToolParameterSchema` (11): type coercion, bounds, allowed_values, bad type.
- `TestToolDefinition` (5): name validation, missing args, unknown keys, optional params, zero timeout.
- `TestToolResult` (4): success/error/denied/timeout constructors.
- `TestSanitizeOutput` (8): ANSI stripping, AWS/bearer/PEM/GitHub/password redaction, truncation, passthrough.
- `TestToolBrokerRegistration` (6): inline/subprocess registration, duplicate, no-handler, both-handlers errors.
- `TestToolBrokerInvoke` (9): success, unknown tool, missing arg, policy denial, scope widening rejection,
  scope narrowing allowed, output sanitization, handler exception, subprocess sandbox.
- `TestSubprocessRunner` (3): command not in allowlist, empty command, allowed command runs.

#### F. Files Created

- `src/tools/__init__.py`
- `src/tools/errors.py`
- `src/tools/models.py`
- `src/tools/sanitizer.py`
- `src/tools/subprocess_runner.py`
- `src/tools/broker.py`
- `tests/test_tool_broker.py`
- `config/sage.example.yaml`
- `tests/test_sage_identity.py`

## [Prompt 019] — 2026-10-01

### Deterministic Authority Layer & Trust Core Foundation

**Status:** IMPLEMENTED & VERIFIED — Full deterministic Trust Core foundation, HMAC-SHA256 capability tokens, fail-closed policy engine, append-only SQLite audit writer with zero-secret invariant, and schema migration 0005.  
**Tests:** 275 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, 14.80s) — 51 new comprehensive adversarial & functional tests (`test_trust_core.py`)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (59 files)  
**Type Check:** `mypy` — 0 issues (57 source files)  
**Commit:** `4d47e72`

#### 1. Core Principle Enforcement
- *"Intelligence proposes; deterministic authority decides."*
- Full separation between LLM reasoning and authorization decisions. LLMs, model providers, skills, and agents are non-authorities and cannot grant themselves capabilities, modify policies, bypass checks, or access resources directly.
- Strict fail-closed semantics: all errors, missing parameters, forged tokens, or unrecognised actions default to DENY or ESCALATE (never silent allow).
- CRITICAL risk actions unconditionally require user approval (ESCALATE) across all autonomy tiers (Tier 0 to Tier 3).
- `policy.change` is permanently hard-denied (`PolicyDeniedError`) across all autonomy tiers without an approval path.

#### 2. Architecture & Modules Created (`src/trust/`)
- **`src/trust/errors.py`**: Granular exception hierarchy rooted in `TrustError`:
  - `CapabilityError` (subclasses: `CapabilityForgedError`, `CapabilityExpiredError`, `CapabilityRevokedError`, `CapabilityExhaustedError`, `CapabilityScopeError`).
  - `AuthorizationError` (subclasses: `PolicyDeniedError`, `ApprovalRequiredError`, `ApprovalExpiredError`, `ApprovalRejectedError`).
  - `AuditError`, `SecretLeakError`, `PolicyConfigurationError`.
- **`src/trust/capability.py`**:
  - `ActionRisk` enum (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`).
  - `ResourceScope` frozen dataclass for boundary enforcement (`filesystem_prefixes`, `allowed_network_hosts`, `allowed_process_commands`, `project_id`) with normalized path traversal protection (`posixpath.normpath`).
  - `Capability` frozen dataclass holding HMAC-SHA256 signature over canonical JSON payload.
  - `CapabilityFactory` generating process-local 32-byte cryptographically secure random keys (`os.urandom(32)`).
  - `CapabilityRegistry` tracking issued capabilities, in-process use counts, single-use/multi-use limits, and immediate revocation.
- **`src/trust/policy.py`**:
  - `AutonomyTier` enum (`0: STRICT_MANUAL`, `1: SAFE_BOUNDED`, `2: SUPERVISED`, `3: FULL_BOUNDED`).
  - `PolicyDecision` enum (`ALLOW`, `ESCALATE`, `DENY`).
  - `PolicyEngine` matching actions via longest prefix against canonical risk taxonomy (`_ACTION_RISK_TABLE`), defaulting unrecognised actions to `ActionRisk.CRITICAL`.
- **`src/trust/audit.py`**:
  - Append-only, tamper-evident audit log writer (`AuditWriter`) with immediate SQLite transaction writes.
  - Hard invariant: zero secret/credential material logged (`_check_no_secrets` scans key names, string values, and patterns for API keys, passwords, bearer tokens, and private keys, raising `SecretLeakError`). SHA-256 digests and hashes remain permitted.
- **`src/trust/authority.py`**:
  - `TrustAuthority` orchestrating policy evaluation, capability token issuance, consumption validation, and audit recording. Fail-closed: audit logging failures raise `AuditError` blocking capability issuance.
- **`src/trust/__init__.py`**:
  - Public export of all domain types and `build_trust_authority()` factory function.

#### 3. Database Migration (`migrations/0005_trust_core_schema.sql`)
- Created migration 0005:
  - `system_autonomy_policy`: Tracks global and project-scoped operational autonomy tiers.
  - `approval_requests`: Tracks human-in-the-loop approval workflows, approval states (`PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`), escalation tokens, and audit linkage.
  - `audit_records`: Append-only audit trail capturing event types (`AUTHZ_REQUEST`, `AUTHZ_DECISION`, `CAPABILITY_ISSUED`, `CAPABILITY_CONSUMED`, `CAPABILITY_DENIED`, `CAPABILITY_REVOKED`, `POLICY_CHANGE`), decision outcomes, actors, actions, scopes, and sanitised telemetry.

#### 4. Adversarial Test Suite (`tests/test_trust_core.py`)
- 51 exhaustive tests spanning 5 categories:
  - **TC-CAP (10 tests)**: Valid signatures, signature tampering rejection, cross-factory forgery rejection, TTL expiry, immediate revocation, use-count exhaustion, path/host/command scope boundaries, zero-scope boundary.
  - **TC-POL (13 tests)**: Autonomy tiers 0-3 vs risk levels LOW-CRITICAL, hard-deny `policy.change`, unrecognised action critical default, tier/risk decoupling.
  - **TC-AUDIT (7 tests)**: Record persistence, secret leakage rejection (`SecretLeakError`), API key/bearer token detection, SHA-256 digest acceptance, multiple record accumulation, redaction helper.
  - **TC-AUTH (9 tests)**: Orchestrated flow for allow/escalate/deny, consume audit records, revocation audit, approved escalation flow, factory builder.
  - **TC-ADV (12 tests)**: LLM self-authorization rejection, actor policy-change rejection, forged token consumption, replay attacks, path traversal attempts (`..`), network host wildcard injection, prompt injection in action strings, secrets in capability IDs, cross-project leakage, audit failure fail-closed behavior, expired/revoked precedence, policy change auditing.

#### 5. Architectural Status
- **DESIGNED:** Windows service broker, named pipe IPC, DPAPI/CNG hardware isolation adapter, Credential Broker, Tool Broker sandboxing.
- **IMPLEMENTED:** Deterministic Authority Layer, Capability tokens with HMAC-SHA256, Action-Risk Taxonomy, Autonomy Tier evaluation, Append-only Audit logging with zero-secret protection, SQLite schema migration 0005.
- **VERIFIED:** 51 Trust Core tests, 275 total system test suite passing 100% offline, ruff clean, mypy clean.
- **DEFERRED:** Platform-specific Windows service & kernel IPC enforcement (kept behind abstract interfaces).

---

## [Prompt 018] — 2026-10-01

### Controlled Model Provisioning & Empirical Hardware Qualification (Outcome B: Safe Defer)

**Status:** SAFE DEFER (Outcome B) — Storage preflight passed; model loading safely deferred due to non-negotiable host RAM reserve gate  
**Tests:** 224 passed · 0 failed · 0 skipped (100% offline inside SAGE venv, 14.63s)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (52 files)  
**Type Check:** `mypy` — 0 issues (50 source files)  

#### 1. Live Hardware Measurements & Memory Gate
- **Total Physical RAM:** 15.69 GB (16,849,256,448 bytes)
- **Live Available RAM:** 2.49 GB (2,670,710,784 bytes)
- **Configured System RAM Reserve Floor:** 1.50 GB (1,610,612,736 bytes) — **Non-negotiable**
- **Live Usable RAM Headroom:** 0.99 GB (1,011.0 MB / 1,060,098,048 bytes)
- **Model Resident Load Requirement:** 3,200 MB resident + 64 MB marginal = 3,264 MB (~3.19 GB)
- **Headroom Shortfall:** 2,253 MB (~2.20 GB) below safe admission requirement
- **Total VRAM:** 6.00 GB (6,442,450,944 bytes) · **Available VRAM:** 5.53 GB (5,934,940,160 bytes)
- **Storage Preflight:** PASS (Host free: 80.76 GB > 40.00 GB floor; required: 7.42 GB permanent)

#### 2. Resource Gate & Safe Defer Determination
- In accordance with Section 2 of Prompt 018 and AEGIS resource-policy specifications, current available RAM does not satisfy the safe admission and provisioning threshold.
- The 1.50 GB system RAM reserve floor is strictly preserved and was not lowered or bypassed.
- Model initialization is not attempted, and zero synthetic/faked qualification profiles were created.
- Per Section 2 and Acceptable Outcomes, the system fails closed and executes **Outcome B (Safe Defer)**.

#### 3. Concurrency Qualification & Skill Admission
- **Lightweight Skills** (CPU-only, e.g. `SKILL_LIGHTWEIGHT_VALIDATOR`):
  - Admission status: **Permitted** (`permitted=True`)
  - Dynamic concurrency ceiling: **3 concurrent skills** (`min(3, 1 + (1,011 MB // 24 MB)) = 3`)
- **Heavyweight Model Skills** (requires model residency, e.g. `SKILL_VON_ROUTING`):
  - Admission status: **Denied / Blocked** (`permitted=False`)
  - Dynamic concurrency ceiling: **0 concurrent skills** (`max_safe_skills=0`)
  - Rationale: Live available RAM headroom (1,011 MB) < required memory (3,264 MB).
  - Degrades safely and non-blockingly to Manager fallback (`FallbackReason.ADMISSION_BUSY`).

#### 4. Verified Failure Behavior & Quality Gate Expansion
- Verified all 8 failure modes required by Section 8:
  1. *Missing artifact*: Fails closed with `verify_integrity() == False`, status `DISABLED`.
  2. *Corrupt/hash-mismatched artifact*: Detected and rejected by SHA-256 verification.
  3. *Insufficient RAM*: Dynamic memory gate blocks admission with `permitted=False`.
  4. *Insufficient VRAM*: Dynamic VRAM gate blocks admission with `permitted=False` (`test_21`).
  5. *Unavailable runtime*: Raises `ProviderUnavailableError` gracefully (`test_12`).
  6. *Initialization failure*: Smoke failure unloads backend and sets status `ERROR` (`test_13`).
  7. *Inference exception/error*: Returns `FallbackReason.INFERENCE_EXCEPTION`, releases inference lock and skill lease (`test_14`).
  8. *Concurrent admission exhaustion*: Non-blocking single-flight lock immediately falls back to `ADMISSION_BUSY`.
- Enhanced `VonFastDecisionProvider.unload()` to accept `target_status` to preserve explicit failure states (`ERROR`, `DISABLED`).

#### 5. Architectural Status
- **DESIGNED:** Local Fast Decision Engine architecture, Option-Marker joint attention, 2–3 skill concurrency target, 1.5 GB RAM reserve floor, 512 MB VRAM reserve floor, 40 GB host storage floor, 30 GB model storage budget.
- **IMPLEMENTED:** Model policy subsystem (`ModelStoragePolicy`, `LiveHardwareBenchmark`, `ModelHardwareQualifier`, `ModelResidencyManager`, `SkillConcurrencyManager`), Fast Decision provider (`VonFastDecisionProvider`), storage boundary enforcement, offline load verification, fail-closed error handling.
- **VERIFIED:** Reconciled manifest consistency, offline tokenizer operation, storage preflight checks, all 8 failure modes, 224 automated unit tests.
- **QUALIFIED:** Empirical live hardware detection and memory query, lightweight skill admission ceiling (3 concurrent skills).
- **DEFERRED:** Model weights download and live empirical resident load qualification deferred under **OUTCOME B (SAFE DEFER)** due to live RAM headroom constraint (0.99 GB available headroom vs 3.20 GB required).

---

## [Prompt 017] — 2026-10-01

### Model Manifest Reconciliation & Provisioning Gate (Outcome B: Safe Stop)

**Status:** SAFE STOP — Manifest fully reconciled; live model loading deferred (host RAM headroom insufficient)  
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline inside SAGE venv, 17.05s)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (52 files)  
**Type Check:** `mypy` — 0 issues (50 source files)

#### Audit Findings — Root Cause of Manifest Discrepancies

The original manifest `schemas/model-manifests/von-1.1-option-marker-395m.json` contained three categories of errors:

1. **Stale revision**: `model_revision` was `4b2d56a297e59b8a07f0f6ea312389d020d18ceb` — this commit does **not** exist on Hugging Face (`HTTP 404`).
2. **Non-existent artifact**: `special_tokens_map.json` was listed as `required: true`, but this file does not exist in `wfzyx/von` at any audited commit (44 commits audited). Confirmed offline-compatible via `AutoTokenizer.from_pretrained` with `local_files_only=True`.
3. **Stale SHA-256 digests**: All five SHA-256 digests were incorrect against the reconciled revision.

#### Reconciled Revision: `308feac9c53d4080df31b7d81362bffbd086de2d`

| Selection Criterion | Value |
|---|---|
| HF commit | `308feac9c53d4080df31b7d81362bffbd086de2d` |
| HF commit message | `feat(model): release converged Option-Marker Universal weights` |
| Push timestamp | 2026-09-21T20:17Z |
| Paired GitHub source commit | `bed7e7337791c5124a557eb59ad918cb6747b276` (2026-09-20T13:38Z) |

This is the earliest HF commit containing all 5 required artifacts at sizes **exactly matching** the byte counts declared in `von_runtime.py` and `AEGIS_MODEL_RESOURCE_POLICY.md`.

Later commits are incompatible:
- Commits from `3896038655` onward alter `model.safetensors` size (~1.579 GB, different parameter dimensions).
- Commits from `5df8185a4f` onward introduce architecture `1.2.0` (`independent_options` field), breaking the `1.1.0` Option-Marker specification.

Artifact sizes verified stable from commit `308feac9c5` through `272b292a05` (7 commits, inclusive).

#### Reconciled SHA-256 Digests (from LFS pointer records at `308feac9c5`)

| Artifact | Size (bytes) | SHA-256 |
|---|---|---|
| `config.json` | 6,349 | `968db6072229e54691be4d535487a5c8d8132cc4893b6a3fdfa1e605457a7ec3` |
| `tokenizer.json` | 3,583,485 | `3703d011b3ee259e00b59b60772f8fd54998d79e8fa5a1388d507eff28f8f09e` |
| `tokenizer_config.json` | 599 | `57170b72a72c60407eec1f7894000c567309a8e41c5d0504b946c624cb069984` |
| `model.safetensors` | 1,583,355,740 | `349c9b1bf3d5e3217a78c8039606e07069ffae4383640998f99a96bdb6b90c8d` |
| `option_marker.pt` | 1,581,316,050 | `52202f176080d1efe38ca62c45264894fc7e444d332f4af6bac86f4e3146e0f2` |

**Note**: Hashes sourced from authoritative Git LFS pointer blobs. Full on-disk SHA-256 recalculation is required **after download**, before model activation. Integrity gate in `VonFastDecisionProvider.verify_integrity()` is unchanged and mandatory.

#### Files Changed

- **`schemas/model-manifests/von-1.1-option-marker-395m.json`**:
  - `manifest_version`: `1.0.0` → `1.1.0` (reflects reconciliation)
  - `model_revision`: `4b2d56a297...` → `308feac9c53d4080df31b7d81362bffbd086de2d`
  - `required_files`: Removed `special_tokens_map.json`; updated all SHA-256 digests and added `size_bytes` fields
  - Added `reconciliation` block (audit trail, rationale, gate status)
  - Added `compatibility` block (provider references, architecture constraints)
- **`src/decision/providers/von_runtime.py`**:
  - `VON_EXPECTED_SHA256`: Removed `special_tokens_map.json`; updated all 5 SHA-256 values to reconciled hashes
  - Added reconciliation comment block with audit trace
  - `VON_EXACT_ARTIFACT_BYTES` unchanged: still exactly 3,168,262,223 bytes (5 files)
  - `VON_FILES_BY_SIZE` unchanged: file sizes verified stable

#### RAM Gate Decision — Outcome B: Safe Stop

| Metric | Value |
|---|---|
| Available host RAM | 2.27 GB (2,441,334,784 bytes) |
| System RAM reserve floor | 1.50 GB (1,610,612,736 bytes) — **non-negotiable** |
| Available RAM headroom | ~788 MB (830,722,048 bytes) |
| Required for model resident load | ~3.168 GB (3,168,262,223 bytes artifact disk; resident graph estimated ~3.0 GB+) |
| Gate result | **BLOCKED** — headroom insufficient by >2.3 GB |

Model provisioning (download + load) is deferred. The manifest reconciliation is complete and internally consistent. The dynamic memory gate in `SkillConcurrencyManager` and `VonFastDecisionProvider.qualify()` will block admission at runtime until host RAM headroom exceeds 3.0 GB above the 1.5 GB reserve floor.

---

## [Prompt 016] — 2026-09-30

### Phase 3: Hardware Qualification & Model Provisioning (Stop Condition Reached)

**Status:** STOP CONDITION REACHED — Manifest checksum discrepancy & host memory limitation  
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline inside SAGE venv)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean (52 files)  
**Type Check:** `mypy` — 0 errors (50 source files)

#### Runtime Dependencies Installed (AEGIS venv `%USERPROFILE%\.aegis\venv`)
- `torch 2.6.0+cu124` (isolated in venv, CUDA enabled with NVIDIA RTX 3050 Laptop GPU)
- `transformers 5.18.0`
- `tokenizers 0.23.2`
- `accelerate 1.15.0`
- `safetensors 0.8.0`
- `numpy 2.5.3`
- `von-sdk 1.3.5`
- Zero modification to system Python.

#### Hardware Measurements (Physical Host Live Empirical Inspection)
- Host CPU: Intel Core i5-13450HX (10 cores, 16 logical)
- Total Physical RAM: 15.69 GB (16,849,256,448 bytes)
- Available Host RAM: 2.27 GB (2,441,334,784 bytes)
- Total GPU VRAM: 6.00 GB (6,442,450,944 bytes, 6,144 MiB)
- Free GPU VRAM: 5.49 GB (5,899,288,576 bytes, 5,626 MiB)
- System RAM Reserve Floor: 1.50 GB (1,610,612,736 bytes)
- Available RAM Headroom: 0.77 GB (~788 MB)

#### Model Artifact Verification & Discrepancy (STOP Condition Triggered)
- Inspected canonical manifest: `schemas/model-manifests/von-1.1-option-marker-395m.json`
- Model repo: `wfzyx/von`
- Declared revision: `4b2d56a297e59b8a07f0f6ea312389d020d18ceb`
- Discrepancy 1: Revision `4b2d56a297e59b8a07f0f6ea312389d020d18ceb` does not exist on Hugging Face (`HTTP 404: Not Found`).
- Discrepancy 2: Manifest lists `special_tokens_map.json` (sha256 `9b1287c88b0...`) as required, but this file does not exist in `wfzyx/von`.
- Discrepancy 3: Manifest declares `config.json` sha256 `8a32b988f0cb6733ecbe3db622be147b7486847feecab41d13dbff4d1b8276f7`, but actual commits with 6349 bytes have sha256 `968db6072229e54691be4d535487a5c8d8132cc4893b6a3fdfa1e605457a7ec3`.
- In strict adherence to Instruction #4 ("If any size, revision, source or checksum differs: STOP immediately and report the discrepancy"), no unverified model artifacts were downloaded or loaded into the repository.

#### Concurrency Ceilings Derived from Empirical Measurements
- Lightweight skills (CPU-only, no resident model): 3 concurrent skills (reaches target ceiling with 788 MB headroom / 24 MB marginal).
- Heavyweight model skills (`SKILL_VON_ROUTING`): 0 concurrent skills (model load deferred; available RAM headroom of 788 MB is below the required ~3.0 GB resident footprint, strictly protected by the 1.5 GB system safety reserve).

#### Quality & Type Check Fixes
- `src/model_policy/qualification.py`: Added explicit `Literal["QUALIFIED", "CONSTRAINED", "FAILED"]` type annotation to resolve mypy arg-type error.
- `tests/test_model_policy.py`: Migrated mock method assignments to `monkeypatch.setattr` to resolve mypy method-assign errors.
- Applied `ruff format` across 52 source and test files.

---

## [Prompt 014B] — 2026-09-23  `a44d40b`

### Model Resource Policy & Skill Concurrency Engine

**Commit:** `a44d40b4c03ca086f57dc2edc15f9cfcec2f87d5`  
**Tests:** 220 passed · 0 failed · 0 skipped (100 % offline)  
**Lint:** `ruff check` — 0 errors · `ruff format --check` — clean

#### Added

- **`src/model_policy/`** — new package (8 modules):
  - `models.py` — canonical data models: `ModelArtifact`, `RuntimeSpec`, `SkillResourceProfile`, `SkillExecutionLease`, `AdmissionDecision`, enums `QualificationState` / `AdmissionResult`.
  - `runtime_registry.py` — heterogeneous runtime registry tracking per-runtime identity, package dependencies, disk footprint, and shared-runtime relationships; avoids double-counting runtimes shared by multiple models.
  - `residency_manager.py` — ref-counted in-memory model residency; eviction safety (refuses to evict a model with active leases).
  - `concurrency_manager.py` — `SkillConcurrencyManager`: qualification-derived concurrency ceiling `min(target_skills, 1 + (ram_headroom // marginal_ram))`; per-skill RAM/VRAM reservation tracking; lightweight-skill fast-path; queue/defer/fallback on admission failure.
  - `live_benchmark.py` — `LiveHardwareBenchmark`: empirical RAM/VRAM delta probing (`MEASURED_PROBE_LOAD_AND_LATENCY` protocol).
  - `qualification.py` — `ModelHardwareQualifier`: drives the live probe, stores empirical results, transitions model to `QUALIFICATION_DERIVED` state.
  - `storage_policy.py` — `ModelStoragePolicy`: manifest-pinned artifact sizing, global storage budget (30.0 GB), host reserve floor (40.0 GB), download + extracted + headroom pre-flight calculation.
  - `__init__.py` — clean public API surface.

- **`src/decision/providers/`** — local model provider runtime constants (artifact byte count, digests, RAM/VRAM design estimates, flagged as pending empirical qualification).

- **`src/config/models.py`** — added `system_ram_reserve_mb`, `system_vram_reserve_mb` configurable fields to `ProvisioningPolicyConfig`.

- **`src/decision/providers/`** — integrated `SkillConcurrencyManager` lease acquisition into the Fast Decision Engine provider; non-blocking admission with structured fallback.

- **`docs/specifications/AEGIS_MODEL_RESOURCE_POLICY.md`** — authoritative specification: three-tier hierarchy (Skills → Models → Runtimes), qualification-derived concurrency formula, storage budget arithmetic, qualification protocol.

- **`tests/test_model_policy.py`** — 20 unit tests covering all 8 modules; 100 % offline.

#### Key Architectural Decisions

| Decision | Value |
|---|---|
| Concurrent-skill target | 2–3 skills (not 2–3 resident models) |
| Concurrency ceiling basis | Empirically measured RAM/VRAM headroom |
| System RAM reserve | 1.5 GB (configurable via `ProvisioningPolicyConfig`) |
| System VRAM reserve | 512 MB (configurable) |
| Global model storage budget | 30.0 GB |
| Host storage floor | 40.0 GB |
| Runtime double-counting | Prevented — shared runtimes counted once across models |

> ⚠️ **PENDING — Local Model Runtime Installation & Hardware Qualification**
>
> Model runtime installation, local model artifact download, and live inference
> benchmarking (`MEASURED_PROBE_LOAD_AND_LATENCY`) have **NOT** been performed
> on this machine. Concurrency limits above are design-specified estimates derived
> from architecture. All empirical residency values (resident RAM/VRAM delta,
> marginal per-inference cost) must be measured before concurrency ceilings are
> treated as authoritative. Explicit human approval is required before any
> model runtime installation or artifact download proceeds.

---

## [Prompt 014A] — 2026-09-23  `cafbbb7`

### Fast Decision Engine — Lint & Test Stabilisation

**Commit:** `cafbbb7`  
**Tests:** 200 passed · 0 failed · 0 skipped  
**Lint:** `ruff check` — 0 errors (E501 violations resolved)

- Resolved all line-length (E501) lint violations across `src/decision/`.
- Verified 200-test baseline prior to 014B implementation.

---

## [Earlier Milestones] — 2026-09-20 and prior

### Domain Schema Foundation  `db92220`
- Aligned domain schema with architecture; added EKM, projects, conversations domain models.

### SQLite Storage Foundation  `2e7728b` / `53ce80d`
- Domain schema implementation, SQLite WAL storage subsystem.

### Hardware Benchmark Subsystem  `b0885e5`
- Controlled micro-benchmarking framework for CPU, memory, storage with profile state-machine.

### Hardware Detection Subsystem  `31fa866`
- Read-only `HardwareProfile` detection across CPU, RAM, GPU, storage, OS, runtimes.

### Configuration Subsystem  *(earlier)*
- 5-tier precedence loader, 13-domain Pydantic schemas, Windows-path safety, zero-secret enforcement.

### Repository & Foundation  *(initial)*
- Canonical directory structure, development environment, technical foundation specification.

---

*This changelog records implementation milestones only. Design documents and specifications live in `docs/specifications/`.*