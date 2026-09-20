"""AEGIS Local SQLite Storage Foundation - Deterministic Migration Framework.

Document Reference: docs/specifications/AEGIS_SQLITE_STORAGE_SUBSYSTEM.md
Constraints:
- schema_migrations is bootstrapped directly by the runner (NOT migration 0001).
- Monotonic version ordering; rejects duplicate versions, duplicate IDs, and checksum mismatches.
- Migration SQL files MUST NOT contain BEGIN/COMMIT/ROLLBACK.
- Migration runner owns transaction boundaries; rolls back completely on any failure.
- Destructive migrations (is_destructive=True) require backup hook precondition; fails closed.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Protocol

from src.storage.database import transaction
from src.storage.errors import (
    ChecksumMismatchError,
    DestructiveMigrationError,
    MigrationError,
)

SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    migration_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    is_destructive INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT NOT NULL
);
"""


class DatabaseBackupHook(Protocol):
    """Callable contract for pre-destructive migration database backup snapshot."""

    def __call__(self, database_path: Path) -> bool:
        """Create a backup of the database before a destructive migration runs.

        Returns True on successful backup completion, False otherwise.
        """
        ...


@dataclass(frozen=True)
class Migration:
    """Defined schema migration unit."""

    version: int
    migration_id: str
    name: str
    sql: str
    checksum: str
    is_destructive: bool = False
    file_path: Optional[Path] = None


@dataclass(frozen=True)
class AppliedMigration:
    """Persisted record of an applied migration from schema_migrations."""

    version: int
    migration_id: str
    name: str
    checksum: str
    is_destructive: bool
    applied_at: str


def compute_sql_checksum(sql: str) -> str:
    """Compute deterministic SHA-256 checksum of SQL content.

    Normalizes newlines and strips surrounding whitespace.
    """
    normalized = "\n".join(line.rstrip() for line in sql.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def execute_sql_script_in_transaction(cursor: sqlite3.Cursor, sql: str) -> None:
    """Execute SQL script statements individually to preserve active transaction boundaries.

    Note: Python sqlite3's cursor.executescript() issues an implicit COMMIT before running,
    breaking user-managed transactions. Executing individual complete statements preserves
    strict atomicity under BEGIN IMMEDIATE / COMMIT.
    """
    buffer = ""
    for line in sql.splitlines():
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("--"):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                cursor.execute(stmt)
            buffer = ""
    remainder = buffer.strip()
    if remainder:
        cursor.execute(remainder)


def bootstrap_schema_migrations(conn: sqlite3.Connection) -> None:
    """Bootstrap the schema_migrations tracking table if it does not exist.

    Must be run before reading or applying migrations to prevent bootstrap circularity.
    """
    conn.execute(SCHEMA_MIGRATIONS_TABLE_SQL)


def get_applied_migrations(conn: sqlite3.Connection) -> List[AppliedMigration]:
    """Fetch list of already-applied migrations ordered by version ascending."""
    bootstrap_schema_migrations(conn)
    cursor = conn.execute(
        "SELECT version, migration_id, name, checksum, is_destructive, applied_at "
        "FROM schema_migrations ORDER BY version ASC;"
    )
    rows = cursor.fetchall()
    return [
        AppliedMigration(
            version=row["version"],
            migration_id=row["migration_id"],
            name=row["name"],
            checksum=row["checksum"],
            is_destructive=bool(row["is_destructive"]),
            applied_at=row["applied_at"],
        )
        for row in rows
    ]


def get_current_schema_version(conn: sqlite3.Connection) -> int:
    """Query current database schema version. Returns 0 if no migrations applied."""
    bootstrap_schema_migrations(conn)
    row = conn.execute("SELECT MAX(version) AS max_v FROM schema_migrations;").fetchone()
    if row is None or row["max_v"] is None:
        return 0
    return int(row["max_v"])


def validate_migration_sequence(migrations: List[Migration]) -> None:
    """Validate that migrations are strictly monotonic, unique, and well-formed.

    Raises:
        MigrationError: If duplicate versions, duplicate IDs, or out-of-order versions exist.
    """
    seen_versions = set()
    seen_ids = set()
    prev_version = 0

    for m in migrations:
        if m.version <= 0:
            raise MigrationError(f"Migration version must be a positive integer: {m.version}")
        if m.version in seen_versions:
            raise MigrationError(f"Duplicate migration version detected: {m.version}")
        if m.migration_id in seen_ids:
            raise MigrationError(f"Duplicate migration ID detected: '{m.migration_id}'")
        if m.version <= prev_version:
            raise MigrationError(
                f"Migrations must be strictly ordered by ascending version. "
                f"Found version {m.version} after {prev_version}."
            )

        seen_versions.add(m.version)
        seen_ids.add(m.migration_id)
        prev_version = m.version


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: List[Migration],
    backup_hook: Optional[Callable[[Path], bool]] = None,
    database_path: Optional[Path] = None,
) -> List[AppliedMigration]:
    """Execute unapplied migrations in strictly ordered atomic transactions.

    Preconditions & Invariants:
    - schema_migrations bootstrapped first.
    - Incoming migration list is validated for strict monotonic order.
    - Previously applied migrations must match existing checksums exactly.
    - Destructive migrations (is_destructive=True) require a backup_hook that succeeds.
    - Each migration runs inside an atomic transaction (BEGIN IMMEDIATE / COMMIT).
    - On error: transaction rolls back completely and migration remains unapplied.

    Returns:
        List of all applied migrations after completion.

    Raises:
        ChecksumMismatchError: If an already-applied migration checksum differs.
        DestructiveMigrationError: If backup precondition is not met for a destructive migration.
        MigrationError: If any migration step or SQL execution fails.
    """
    bootstrap_schema_migrations(conn)
    validate_migration_sequence(migrations)
    applied = get_applied_migrations(conn)

    # 1. Verify history of already applied migrations
    for i, recorded in enumerate(applied):
        if i >= len(migrations):
            raise MigrationError(
                f"Database has migration v{recorded.version} ('{recorded.migration_id}'), "
                "not present in the provided migration set."
            )
        candidate = migrations[i]
        if recorded.version != candidate.version or recorded.migration_id != candidate.migration_id:
            raise MigrationError(
                f"Migration sequence mismatch at index {i}: recorded "
                f"v{recorded.version} ('{recorded.migration_id}') vs candidate "
                f"v{candidate.version} ('{candidate.migration_id}')."
            )
        if recorded.checksum != candidate.checksum:
            raise ChecksumMismatchError(
                f"Checksum mismatch for migration '{candidate.migration_id}' "
                f"(v{candidate.version}): recorded '{recorded.checksum}' vs "
                f"definition computes '{candidate.checksum}'."
            )

    # 2. Execute pending migrations
    pending = migrations[len(applied) :]
    for m in pending:
        # Check destructive safety precondition
        if m.is_destructive:
            if backup_hook is None:
                raise DestructiveMigrationError(
                    f"Destructive migration '{m.migration_id}' requires a registered backup hook."
                )
            if database_path is None:
                raise DestructiveMigrationError(
                    f"Destructive migration '{m.migration_id}' requires database_path for backup."
                )
            try:
                hook_ok = backup_hook(database_path)
            except Exception as exc:
                raise DestructiveMigrationError(
                    f"Backup hook raised exception for destructive migration "
                    f"'{m.migration_id}': {exc}"
                ) from exc

            if not hook_ok:
                raise DestructiveMigrationError(
                    f"Backup precondition failed (hook returned False) for "
                    f"destructive migration '{m.migration_id}'."
                )

        # Migration SQL files must not contain transaction control
        for disallowed in ("BEGIN", "COMMIT", "ROLLBACK"):
            pattern = rf"\b{disallowed}\b"
            if re.search(pattern, m.sql, re.IGNORECASE):
                raise MigrationError(
                    f"Migration '{m.migration_id}' contains forbidden control '{disallowed}'. "
                    "The migration runner owns all transaction boundaries."
                )

        # Execute atomically
        try:
            with transaction(conn, immediate=True) as cursor:
                execute_sql_script_in_transaction(cursor, m.sql)
                applied_time = datetime.now(timezone.utc).isoformat()
                insert_sql = (
                    "INSERT INTO schema_migrations "
                    "(version, migration_id, name, checksum, is_destructive, applied_at) "
                    "VALUES (?, ?, ?, ?, ?, ?);"
                )
                cursor.execute(
                    insert_sql,
                    (
                        m.version,
                        m.migration_id,
                        m.name,
                        m.checksum,
                        1 if m.is_destructive else 0,
                        applied_time,
                    ),
                )
        except Exception as exc:
            if isinstance(exc, (ChecksumMismatchError, DestructiveMigrationError, MigrationError)):
                raise
            raise MigrationError(
                f"Execution failed for migration '{m.migration_id}' (v{m.version}): {exc}"
            ) from exc

    return get_applied_migrations(conn)


def parse_migration_file(file_path: Path) -> Migration:
    """Parse a .sql migration file and extract version, ID, name, and SQL text."""
    filename = file_path.name
    match = re.match(r"^(\d+)_(.+)\.sql$", filename)
    if not match:
        raise MigrationError(
            f"Invalid migration filename format: '{filename}'. Expected '0001_name.sql'."
        )

    version = int(match.group(1))
    name = match.group(2)
    migration_id = f"{version:04d}_{name}"
    sql = file_path.read_text(encoding="utf-8")

    # Check for header metadata comments
    is_destructive = False
    for line in sql.splitlines():
        if line.strip().lower().startswith("-- destructive:"):
            val = line.split(":", 1)[1].strip().lower()
            is_destructive = val in ("true", "1", "yes")

    checksum = compute_sql_checksum(sql)

    return Migration(
        version=version,
        migration_id=migration_id,
        name=name,
        sql=sql,
        checksum=checksum,
        is_destructive=is_destructive,
        file_path=file_path,
    )


def load_migrations_from_directory(dir_path: Path) -> List[Migration]:
    """Discover and parse all migration files from a directory, sorted by version."""
    if not dir_path.is_dir():
        return []

    sql_files = sorted(dir_path.glob("*.sql"), key=lambda p: p.name)
    migrations = [parse_migration_file(f) for f in sql_files]
    validate_migration_sequence(migrations)
    return migrations


def get_builtin_migrations() -> List[Migration]:
    """Load default migration set from canonical migrations/ directory."""
    canonical_root = Path(__file__).resolve().parent.parent.parent
    migrations_dir = canonical_root / "migrations"
    return load_migrations_from_directory(migrations_dir)
