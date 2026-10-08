"""SAGE Local SQLite Storage Foundation.

Document Reference: docs/specifications/AEGIS_SQLITE_STORAGE_SUBSYSTEM.md
Constraints:
- Standard library sqlite3 only (zero external ORMs or database servers).
- Runtime database strictly decoupled from OneDrive, Git repo, and recovery copy.
- Explicit transaction management and monotonic migration engine with backup safety hooks.
"""

from src.storage.database import (
    SQLitePragmaConfig,
    create_connection,
    initialize_runtime_database,
    open_database,
    transaction,
)
from src.storage.errors import (
    ChecksumMismatchError,
    DatabaseConnectionError,
    DatabaseCorruptError,
    DatabaseLockedError,
    DestructiveMigrationError,
    ForbiddenDatabaseLocationError,
    InsufficientStorageError,
    MigrationError,
    StorageError,
    TransactionError,
)
from src.storage.health import (
    DatabaseHealthReport,
    DatabaseHealthState,
    check_database_health,
)
from src.storage.migrations import (
    AppliedMigration,
    DatabaseBackupHook,
    Migration,
    apply_migrations,
    bootstrap_schema_migrations,
    compute_sql_checksum,
    get_applied_migrations,
    get_builtin_migrations,
    get_current_schema_version,
    load_migrations_from_directory,
)
from src.storage.paths import (
    get_default_aegis_home,
    get_default_backups_dir,
    get_default_cache_dir,
    get_default_data_dir,
    get_default_db_path,
    get_default_logs_dir,
    get_default_runtime_dir,
    get_default_sage_home,
    is_path_inside_onedrive,
    is_path_inside_recovery_repo,
    is_path_inside_repo,
    resolve_database_path,
    validate_database_path,
)
from src.storage.trust_levels import (
    MEMORY_TRUST_LEVEL_SEMANTICS,
    NON_CANONICAL_TRUST_ALIASES,
    MemoryTrustLevel,
    get_trust_level_semantic,
    resolve_trust_level,
)

__all__ = [
    # Errors
    "StorageError",
    "ForbiddenDatabaseLocationError",
    "DatabaseConnectionError",
    "DatabaseLockedError",
    "DatabaseCorruptError",
    "MigrationError",
    "ChecksumMismatchError",
    "DestructiveMigrationError",
    "TransactionError",
    "InsufficientStorageError",
    # Paths
    "get_default_aegis_home",
    "get_default_sage_home",
    "get_default_data_dir",
    "get_default_db_path",
    "get_default_runtime_dir",
    "get_default_logs_dir",
    "get_default_cache_dir",
    "get_default_backups_dir",
    "resolve_database_path",
    "validate_database_path",
    "is_path_inside_onedrive",
    "is_path_inside_repo",
    "is_path_inside_recovery_repo",
    # Database & Transactions
    "SQLitePragmaConfig",
    "create_connection",
    "initialize_runtime_database",
    "open_database",
    "transaction",
    # Migrations
    "Migration",
    "AppliedMigration",
    "DatabaseBackupHook",
    "bootstrap_schema_migrations",
    "get_applied_migrations",
    "get_current_schema_version",
    "apply_migrations",
    "load_migrations_from_directory",
    "get_builtin_migrations",
    "compute_sql_checksum",
    # Health
    "DatabaseHealthState",
    "DatabaseHealthReport",
    "check_database_health",
    # Trust Levels
    "MemoryTrustLevel",
    "MEMORY_TRUST_LEVEL_SEMANTICS",
    "NON_CANONICAL_TRUST_ALIASES",
    "resolve_trust_level",
    "get_trust_level_semantic",
]
