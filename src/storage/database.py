"""SAGE Local SQLite Storage Foundation - Connection & Transaction Management.

Document Reference: docs/specifications/AEGIS_SQLITE_STORAGE_SUBSYSTEM.md
Constraints:
- Pure standard library sqlite3 (zero ORMs, zero external dependencies).
- Strict PRAGMA configuration and verification (foreign keys, WAL, normal sync).
- Deterministic transaction context manager with rollback and nested transaction rejection.
- Scoped connection lifecycle (no global mutable singletons).
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

from src.storage.errors import (
    DatabaseConnectionError,
    DatabaseLockedError,
    TransactionError,
)


@dataclass(frozen=True)
class SQLitePragmaConfig:
    """Explicit SQLite connection PRAGMA configuration.

    Policy rationale:
    - busy_timeout_ms (default 5000): Gracefully retries locked databases on Windows
      filesystems before giving up.
    - journal_mode (default WAL): Enables Write-Ahead Logging for non-blocking concurrent
      readers and writers. Note: ':memory:' databases use MEMORY journal mode.
    - synchronous (default NORMAL): In WAL mode, NORMAL synchronous mode preserves durability
      across OS/app crashes while eliminating excessive fsync bottlenecks.
    - foreign_keys (default True): Crucial relational integrity; SQLite defaults to OFF.
    - temp_store (default MEMORY): Directs scratch tables and temporary sorting buffers to RAM.
    """

    busy_timeout_ms: int = 5000
    journal_mode: str = "WAL"
    synchronous: str = "NORMAL"
    foreign_keys: bool = True
    temp_store: str = "MEMORY"


def create_connection(
    db_path: Path | str,
    pragma_config: Optional[SQLitePragmaConfig] = None,
    timeout_sec: float = 5.0,
) -> sqlite3.Connection:
    """Initialize and configure a robust, scoped SQLite connection.

    Uses isolation_level=None (autocommit mode) so that explicit transaction
    statements (BEGIN, COMMIT, ROLLBACK) are handled with deterministic precision.

    Raises:
        DatabaseConnectionError: If connection or PRAGMA initialization fails.
        DatabaseLockedError: If database file is locked during connection.
    """
    cfg = pragma_config or SQLitePragmaConfig()
    path_str = str(db_path)
    is_memory = path_str == ":memory:" or path_str.startswith("file::memory:")

    try:
        # Connect with configured busy timeout
        conn = sqlite3.connect(
            path_str,
            timeout=timeout_sec,
            isolation_level=None,  # Manual deterministic transaction management
        )
        conn.row_factory = sqlite3.Row

        # 1. Foreign Keys
        if cfg.foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON;")
            row = conn.execute("PRAGMA foreign_keys;").fetchone()
            if row is None or row[0] != 1:
                raise DatabaseConnectionError("Failed to enable PRAGMA foreign_keys = ON.")

        # 2. Busy Timeout
        conn.execute(f"PRAGMA busy_timeout = {int(cfg.busy_timeout_ms)};")

        # 3. Temp Store
        conn.execute(f"PRAGMA temp_store = {cfg.temp_store};")

        # 4. Journal Mode
        # SQLite does not support WAL on pure in-memory databases
        target_journal = "MEMORY" if is_memory else cfg.journal_mode
        actual_journal = conn.execute(f"PRAGMA journal_mode = {target_journal};").fetchone()[0]
        if not is_memory and cfg.journal_mode.upper() == "WAL":
            if str(actual_journal).upper() != "WAL":
                raise DatabaseConnectionError(
                    f"Failed to set WAL journal mode. Effective mode: '{actual_journal}'"
                )

        # 5. Synchronous Mode
        conn.execute(f"PRAGMA synchronous = {cfg.synchronous};")

        return conn

    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            raise DatabaseLockedError(f"Database lock contention on '{path_str}': {exc}") from exc
        raise DatabaseConnectionError(
            f"Failed to establish SQLite connection to '{path_str}': {exc}"
        ) from exc
    except Exception as exc:
        if isinstance(exc, (DatabaseConnectionError, DatabaseLockedError)):
            raise
        raise DatabaseConnectionError(
            f"Unexpected error connecting to SQLite database at '{path_str}': {exc}"
        ) from exc


@contextmanager
def open_database(
    db_path: Path | str,
    pragma_config: Optional[SQLitePragmaConfig] = None,
    timeout_sec: float = 5.0,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for scoped SQLite connections.

    Guarantees clean shutdown and closing upon scope exit.
    Enforces the architectural invariant:
    'One connection should not become a global mutable singleton.'
    """
    conn = create_connection(db_path, pragma_config=pragma_config, timeout_sec=timeout_sec)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def transaction(
    conn: sqlite3.Connection,
    immediate: bool = True,
) -> Generator[sqlite3.Cursor, None, None]:
    """Deterministic transaction context manager.

    Behaviors:
    - Rejects illegal nested transactions with TransactionError.
    - Emits BEGIN IMMEDIATE (default) or BEGIN.
    - Commits on normal block exit.
    - Rolls back on unhandled exception and preserves original exception.
    """
    if conn.in_transaction:
        raise TransactionError(
            "Nested transaction rejected: A transaction is already active on this connection. "
            "Use sequential transaction boundaries or explicit savepoints."
        )

    begin_cmd = "BEGIN IMMEDIATE;" if immediate else "BEGIN;"
    cursor = conn.cursor()
    cursor.execute(begin_cmd)

    try:
        yield cursor
        cursor.execute("COMMIT;")
    except Exception:
        try:
            cursor.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        cursor.close()
