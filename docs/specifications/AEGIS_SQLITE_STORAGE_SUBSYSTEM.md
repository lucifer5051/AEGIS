# AEGIS Local SQLite Storage Subsystem Specification

**Document Version**: 1.0.0-FROZEN-STORAGE-FOUNDATION  
**Subsystem**: AEGIS Local SQLite Storage Foundation (`src/storage/`)  
**Phase**: LOCAL_PERSISTENCE_RUNTIME  
**Prompt**: 011  
**Status**: APPROVED & IMPLEMENTED  
**Date**: 2026-09-20  
**Canonical Project Root**: `C:\Users\digvi\OneDrive\Desktop\project\aeigs`  
**Recovery Copy Target**: `C:\AEGIS` (Strictly untouched)  

---

## 1. Purpose & Architectural Role

The AEGIS Local SQLite Storage Subsystem provides the foundational, machine-first relational persistence runtime for the AEGIS autonomous intelligence platform.

Its scope encompasses:
- Deterministic database path resolution outside cloud-synchronized and source-controlled directories.
- Standard-library SQLite connection lifecycle management with strict relational integrity (`PRAGMA foreign_keys = ON`).
- Concurrency and durability optimization using Write-Ahead Logging (`PRAGMA journal_mode = WAL`) and crash-safe synchronous flushing (`PRAGMA synchronous = NORMAL`).
- Explicit, atomic transaction boundaries that cleanly commit or roll back on errors while rejecting illegal nested transactions.
- A robust, monotonic schema migration engine that bootstraps metadata, verifies cryptographic checksums, and requires verified backup preconditions prior to executing destructive changes.
- Comprehensive 12-point health evaluation that classifies storage states into `HEALTHY`, `DEGRADED`, `CORRUPT`, or `UNAVAILABLE` without mistaking lock contention for corruption.

This subsystem provides pure storage infrastructure. It hosts zero domain business logic (e.g. Second Brain EKM, tasks, workflows, audits, skills, or model provisioning).

---

## 2. Runtime Location & Directory Topology

The default physical location of the active SQLite database is deterministically anchored to the local user profile:

$$\text{Database Path} = \%USERPROFILE\%\backslash.aegis\backslash data\backslash aegis.db$$

```text
%USERPROFILE%\.aegis\
├── data\
│   ├── aegis.db          <- Canonical SQLite database file
│   ├── aegis.db-wal      <- Write-Ahead Log journal (transient)
│   └── aegis.db-shm      <- Shared memory index (transient)
├── runtime\              <- Ephemeral execution state & staging
├── logs\                 <- Diagnostic and operational event logs
├── cache\                <- Derived vector embeddings & LRU caches
└── backups\              <- Pre-migration recovery snapshots
```

Only directories actively required by currently initialized subsystems are created on the host filesystem.

---

## 3. Critical OneDrive Separation Rationale

The canonical Git repository resides at:
`C:\Users\digvi\OneDrive\Desktop\project\aeigs`

The active SQLite database is **strictly prohibited** from residing inside the repository, inside `C:\AEGIS`, or anywhere within the Microsoft OneDrive filesystem tree.

### Root Technical Drivers:
1. **SQLite WAL Churn**: In WAL mode, transactions generate rapid, microsecond-level append and checkpoint writes across `aegis.db-wal` and `aegis.db-shm`. Cloud file synchronizers attempt to hash and upload each change continuously, flooding host I/O and network bandwidth.
2. **File Locking Contention**: Windows file locks (`LockFileEx`) managed by SQLite collide with OneDrive background scanners. When OneDrive temporarily locks a database file during sync, SQLite throws `sqlite3.OperationalError: database is locked`.
3. **Synchronization Races & Split-Brain Corruption**: If multiple devices or concurrent processes interact with a synced database file, cloud conflict copies (e.g. `aegis-DESKTOP.db`) are created, breaking ACID guarantees and leading to unrecoverable database corruption.
4. **Git Interaction & Repository Pollution**: Active runtime databases in Git repositories cause unintended working-tree dirty states, accidental commits of transient states, and Git index bloat.
5. **Disk Write Latency**: Synchronous filesystem filter drivers installed by cloud sync engines intercept disk writes, significantly degrading transaction response times.

---

## 4. Source / Repository vs. Runtime Data Separation

| Asset Category | Location | Management Authority | Git Tracked |
| :--- | :--- | :--- | :--- |
| **Source Code** | `C:\Users\digvi\OneDrive\Desktop\project\aeigs\src\` | Git / Developer | Yes |
| **Migration SQL** | `C:\Users\digvi\OneDrive\Desktop\project\aeigs\migrations\` | Git / Developer | Yes |
| **Schema Manifests** | `C:\Users\digvi\OneDrive\Desktop\project\aeigs\schemas\` | Git / Developer | Yes |
| **Active Database** | `%USERPROFILE%\.aegis\data\aegis.db` | AEGIS Storage Runtime | **NO** (Strictly Ignored) |
| **WAL & SHM Files** | `%USERPROFILE%\.aegis\data\aegis.db-*` | SQLite Engine | **NO** (Strictly Ignored) |
| **Pre-Migration Backups** | `%USERPROFILE%\.aegis\backups\` | Storage Backup Hook | **NO** (Strictly Ignored) |

---

## 5. SQLite Engine Policy

1. **Standard Library Exclusivity**: AEGIS relies exclusively on Python's built-in `sqlite3` engine.
2. **Zero External Dependencies**:
   - No ORMs (SQLAlchemy, Peewee, Tortoise).
   - No database servers (PostgreSQL, MySQL).
   - No NoSQL/document engines (MongoDB, Redis).
3. **Deterministic Connection Mode**:
   Connections are opened with `isolation_level=None` (autocommit mode). This eliminates Python's implicit transaction state machine and gives the AEGIS storage foundation total, deterministic control over `BEGIN IMMEDIATE`, `COMMIT`, and `ROLLBACK`.

---

## 6. PRAGMA Policy & Effective Settings

Every newly initialized SQLite connection undergoes deterministic PRAGMA configuration:

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
```

### Trade-off & Rationale Analysis:
- `foreign_keys = ON`: SQLite disables foreign key validation by default for backward compatibility. AEGIS mandates referential integrity across all entities; connections verify `PRAGMA foreign_keys` evaluates to `1`.
- `busy_timeout = 5000`: Sets a 5,000 ms retry threshold on Windows file locks, preventing spurious failures under short-lived concurrent reads.
- `journal_mode = WAL`: Write-Ahead Logging allows readers to execute simultaneously with writers without blocking. On in-memory databases (`:memory:`), SQLite automatically falls back to `MEMORY` journal mode.
- `synchronous = NORMAL`: In WAL mode, `NORMAL` ensures total database integrity across power interruptions and application crashes without issuing full sync barriers on every transaction, boosting write throughput.
- `temp_store = MEMORY`: Keeps temporary tables and sorting structures in RAM, eliminating transient disk writes.

---

## 7. Connection Lifecycle & Scoping

To prevent resource leaks and cross-thread concurrency anomalies:
- **No Global Mutable Singletons**: A single SQLite connection must never be stored as a global module-level variable.
- **Scoped Context Management**: Connections are opened via `open_database(db_path, pragma_config)`, which guarantees clean shutdown and `close()` on block exit.
- **Deterministic Row Access**: Connections set `conn.row_factory = sqlite3.Row`, enabling case-preserving column lookup.

---

## 8. Transaction Model

Transactions are managed through the `@contextmanager def transaction(conn, immediate=True)` abstraction:

```python
with transaction(conn) as cursor:
    cursor.execute("INSERT INTO storage_metadata ...")
```

- **Execution Flow**:
  1. Emits `BEGIN IMMEDIATE;` by default to acquire write intent locks early, preventing deadlocks.
  2. Yields the active `sqlite3.Cursor`.
  3. Executes `COMMIT;` upon normal completion.
  4. Upon any unhandled exception, emits `ROLLBACK;` and cleanly re-raises the original exception without modification or obfuscation.

---

## 9. Nested Transaction Behavior

SQLite does not support true nested `BEGIN` statements. Calling `BEGIN` while a transaction is active produces `sqlite3.OperationalError: cannot start a transaction within a transaction`.

**Design Choice**: AEGIS strictly **rejects** nested transaction invocations:
- If `transaction()` is called when `conn.in_transaction` is true, it immediately raises `TransactionError`.
- This fail-closed invariant prevents hidden transaction leaks, accidental auto-commits, and unexpected rollback cascades.

---

## 10. Migration Framework Architecture & Bootstrap

To eliminate circular bootstrap dependencies, `schema_migrations` is **not** created by migration `0001`. The migration runner directly bootstraps the table using `CREATE TABLE IF NOT EXISTS`:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    migration_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    is_destructive INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT NOT NULL
);
```

Migration `0001_initial_storage.sql` contains only the minimal non-business `storage_metadata` table:

```sql
CREATE TABLE IF NOT EXISTS storage_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

---

## 11. Migration Ordering & Validation Rules

Migrations must satisfy strict ordering invariants:
1. Version numbers must be positive integers ($v \ge 1$).
2. Version numbers must be strictly ascending with zero duplicates.
3. Migration IDs must be globally unique.
4. Migration SQL files must **never** contain explicit transaction statements (`BEGIN`, `COMMIT`, `ROLLBACK`); transaction boundaries belong exclusively to the runner.

---

## 12. Cryptographic Checksum Verification

Every migration SQL payload is hashed using SHA-256 after newline normalization:

$$\text{Checksum} = \text{SHA-256}(\text{normalized\_sql})$$

During startup:
- The runner fetches all previously recorded migrations from `schema_migrations`.
- It verifies that each recorded checksum exactly matches the current code definition.
- If any discrepancy is detected, execution halts with `ChecksumMismatchError`. Under no circumstances are recorded checksums silently overwritten.

---

## 13. Destructive Migration Safety & Preconditions

Migrations modifying schemas destructively (e.g. dropping tables, removing columns, altering integrity constraints) must declare `is_destructive = True`.

### Precondition Enforcement:
When a destructive migration is detected, the runner mandates:
1. A registered `DatabaseBackupHook` must be supplied.
2. The hook must execute and return `True` before the migration transaction begins.
3. If the hook is absent or returns `False`, execution raises `DestructiveMigrationError` and aborts.

```text
[Pending Migration]
       │
  is_destructive?
    ├── NO  ──> [BEGIN IMMEDIATE] ──> [Run SQL] ──> [Record Version] ──> [COMMIT]
    └── YES ──> [Backup Hook Registered?]
                   ├── NO  ──> Fail Closed (DestructiveMigrationError)
                   └── YES ──> [Execute Backup Snapshot]
                                  ├── FAILED ──> Fail Closed (DestructiveMigrationError)
                                  └── SUCCESS ──> [BEGIN IMMEDIATE] ──> [Run SQL] ──> [COMMIT]
```

---

## 14. Backup Hook Abstraction

The backup interface is defined as a Python Protocol:

```python
class DatabaseBackupHook(Protocol):
    def __call__(self, database_path: Path) -> bool:
        ...
```

Prompt 011 establishes the architectural contract. Cloud backup services (Google Drive, remote synchronization, encryption) are strictly deferred to future subsystems.

---

## 15. Comprehensive 12-Point Health Evaluation

The `check_database_health(db_path, pragma_config)` diagnostic engine verifies:

1. **Path Resolution**: Target path conforms to absolute Windows formatting.
2. **File Accessibility**: File exists and permits read/write permissions.
3. **Connection Establishment**: Engine establishes connection within busy timeout.
4. **Foreign Key Enforcement**: `PRAGMA foreign_keys` evaluates to `1`.
5. **Journal Mode**: Evaluates to `WAL` for file-backed storage (or `MEMORY` for `:memory:`).
6. **Synchronous Mode**: Conforms to `NORMAL` (or configured setting).
7. **Temp Store**: Evaluates to `MEMORY`.
8. **Schema Version**: Reads latest applied version integer.
9. **Migration Count**: Counts recorded entries in `schema_migrations`.
10. **Transaction Capability**: Probes read/write capability via a transient temporary table.
11. **Structural Integrity**: Executes `PRAGMA integrity_check(10)` and verifies output is `["ok"]`.
12. **Disk Free Space**: Queries host storage volume free space against safety limits.

### Health State Classification:
- **`HEALTHY`**: All 12 inspection points pass verified safe standards.
- **`DEGRADED`**: Database is accessible, but non-critical condition impaired (e.g. non-WAL journal, low free disk space).
- **`CORRUPT`**: Structural integrity check fails or SQLite returns database malformation errors.
- **`UNAVAILABLE`**: File does not exist, access is denied, or database is locked.

---

## 16. Corruption & Failure Handling

- **Fail-Safe Principle**: If a database is classified as `CORRUPT`, AEGIS **never** silently deletes or automatically recreates the database file.
- **Diagnostic Logging**: Corruption messages from `PRAGMA integrity_check` are surfaced in `DatabaseHealthReport.diagnostic_messages`.
- **Transaction Rollback**: Migration and operational failures roll back the active transaction, leaving the database in its previous valid state.

---

## 17. Disk Space Hygiene

- Storage operations inspect host storage volume free space using `shutil.disk_usage`.
- If free storage drops below `MIN_SAFE_DISK_SPACE_BYTES` (50 MB), the database health state is marked `DEGRADED` with explicit diagnostic alerts to prevent sudden SQLite I/O write failures.

---

## 18. Security & Zero-Secret Invariants

1. **No Credentials in Storage Foundation**: The storage foundation metadata tables (`schema_migrations`, `storage_metadata`) store strictly operational attributes. Passwords, API tokens, OAuth keys, and private keys are never written to database tables.
2. **Credential Broker Isolation**: Secure credential management will be handled exclusively by the Credential Broker in subsequent phases.
3. **No Dynamic SQL String Concatenation**: Parameterized queries are enforced across all database queries to eliminate SQL injection vulnerabilities.

---

## 19. Performance Constraints

- **Direct Native Execution**: Python standard library $\to$ `sqlite3` $\to$ C-level SQLite engine.
- **Zero ORM Overhead**: Eliminates object mapping latency, reflection, and hidden N+1 queries.
- **Short Transaction Windows**: Write transactions are kept strictly bounded to prevent write lock starvation.

---

## 20. Testing Strategy

The test suite (`tests/test_storage.py`) enforces 52 deterministic, offline tests:
- **Path Tests (1–11)**: Boundary enforcement outside OneDrive, Git root, and `C:\AEGIS`.
- **Connection Tests (12–21)**: PRAGMA validation, WAL mode, row factory, scoped lifecycle.
- **Transaction Tests (22–27)**: Atomic commit, exception rollback, nested transaction rejection, isolation.
- **Migration Tests (28–42)**: Bootstrap, monotonic ordering, checksum mismatches, failure rollback, backup hooks.
- **Health Tests (43–48)**: 12-point health evaluation, corruption detection, integrity verification.
- **Security Tests (49–51)**: Zero-secret validation, prohibition of domain business tables.
- **Smoke Test (52)**: Full end-to-end temporary database lifecycle.

All tests utilize ephemeral temporary directories; no test ever touches the active production path `%USERPROFILE%\.aegis\data\aegis.db`.

---

## 21. Future Domain Schema Integration Roadmap

Subsequent prompt phases will implement domain-specific business tables on top of this storage foundation:

```text
Prompt 011 (Current):
  └── Storage Foundation (schema_migrations, storage_metadata)

Future Prompts:
  ├── Domain A: Second Brain EKM (memory_facts, entities, relationships)
  ├── Domain B: Tasks & Workflows (projects, tasks, workflow_runs, schedules)
  ├── Domain C: Interactions (conversations, messages, notifications)
  └── Domain D: Security & Audit (audit_records, approvals, autonomy_policy)
```
Each domain schema will be introduced as an incremental, monotonically numbered migration managed by this migration engine.
