"""SAGE Local SQLite Storage Foundation - Domain Exceptions.

Document Reference: docs/specifications/AEGIS_SQLITE_STORAGE_SUBSYSTEM.md
Constraints:
- Strictly typed hierarchical exceptions.
- Preserves root cause exceptions without silent swallowing.
- Fails closed on corruption, security violations, and migration discrepancies.
"""


class StorageError(Exception):
    """Base exception for all SAGE storage foundation operations."""


class ForbiddenDatabaseLocationError(StorageError):
    """Raised when database path resolves inside OneDrive, Git repo, or recovery directories."""


class DatabaseConnectionError(StorageError):
    """Raised when database connection initialization, pragma setup, or access fails."""


class DatabaseLockedError(DatabaseConnectionError):
    """Raised when database lock contention exceeds configured busy timeout."""


class DatabaseCorruptError(StorageError):
    """Raised when database file integrity check fails or structure is malformed."""


class MigrationError(StorageError):
    """Raised when migration execution, version ordering, or transaction fails."""


class ChecksumMismatchError(MigrationError):
    """Raised when an already applied migration has a modified SQL body or checksum."""


class DestructiveMigrationError(MigrationError):
    """Raised when a destructive migration is attempted without required backup precondition."""


class TransactionError(StorageError):
    """Raised when transaction management, boundaries, or illegal nesting occurs."""


class InsufficientStorageError(StorageError):
    """Raised when host volume free space is below the safe threshold for storage operations."""
