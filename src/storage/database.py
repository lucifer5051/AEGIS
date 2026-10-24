"""SAGE Local SQLite Storage Foundation - Connection, Encryption & Transaction Management.

Document Reference: docs/specifications/AEGIS_SQLITE_STORAGE_SUBSYSTEM.md
                    docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md
Constraints:
- SQLCipher 4.19.0 Community integration (via sqlcipher3) for whole-database page encryption.
- Encrypted database pages and structural metadata (AES-256-CBC, 256-bit PBKDF2/HMAC-SHA512).
- Pure standard library sqlite3 fallback/interop where encryption is disabled.
- Fail-closed initialization: missing/invalid keys prevent protected database access.
- Strict PRAGMA configuration and verification (foreign keys, WAL, normal sync).
- Deterministic transaction context manager with rollback and nested transaction rejection.
- Scoped connection lifecycle (no global mutable singletons).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Union
from uuid import uuid4

try:
    import sqlcipher3
    from sqlcipher3 import dbapi2 as sqlcipher_dbapi2

    SQLCIPHER_AVAILABLE = True
except ImportError:
    sqlcipher3 = None  # type: ignore[assignment]
    sqlcipher_dbapi2 = None  # type: ignore[assignment]
    SQLCIPHER_AVAILABLE = False

from src.storage.errors import (
    DatabaseConnectionError,
    DatabaseDrainingError,
    DatabaseLockedError,
    TransactionError,
)

logger = logging.getLogger(__name__)


def rotate_database_key(
    db_path: Path | str,
    key_manager: Any = None,
    new_key: Optional[bytes] = None,
    backup_dir: Optional[Path | str] = None,
    timeout_sec: float = 30.0,
    drain_timeout_sec: float = 15.0,
) -> Dict[str, Any]:
    """Execute coordinated, atomic, and recoverable database key rotation."""
    from src.security.database_key_rotation import rotate_database_key as _rotate

    return _rotate(
        db_path=db_path,
        key_manager=key_manager,
        new_key=new_key,
        backup_dir=backup_dir,
        timeout_sec=timeout_sec,
        drain_timeout_sec=drain_timeout_sec,
    )


def recover_interrupted_rotation(
    db_path: Path | str,
    key_manager: Any = None,
    release_drain: bool = True,
) -> Dict[str, Any]:
    """Inspect and deterministically recover from an interrupted database key rotation."""
    from src.security.database_key_rotation import recover_interrupted_rotation as _recover

    return _recover(db_path=db_path, key_manager=key_manager, release_drain=release_drain)


def __getattr__(name: str) -> Any:
    """Lazy export of key rotation classes to avoid circular imports."""
    if name in (
        "ExclusiveDatabaseLock",
        "RotationDrainTimeoutError",
        "RotationError",
        "RotationJournal",
        "RotationLockError",
        "RotationState",
    ):
        import src.security.database_key_rotation as dkr

        return getattr(dkr, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


# Connection type alias encompassing both standard sqlite3 and sqlcipher3
SQLiteConnection = Union[sqlite3.Connection, Any]

# Unified error tuples to handle exceptions from either sqlite3 or sqlcipher3
if SQLCIPHER_AVAILABLE:
    IntegrityError = (sqlite3.IntegrityError, sqlcipher3.IntegrityError)  # type: ignore[assignment]
    OperationalError = (sqlite3.OperationalError, sqlcipher3.OperationalError)  # type: ignore[assignment]
    DatabaseError = (sqlite3.DatabaseError, sqlcipher3.DatabaseError)  # type: ignore[assignment]
else:
    IntegrityError = (sqlite3.IntegrityError,)  # type: ignore[assignment]
    OperationalError = (sqlite3.OperationalError,)  # type: ignore[assignment]
    DatabaseError = (sqlite3.DatabaseError,)  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Write-statement detection for drain-gate enforcement (SEC-044-01/02/03 fix)
# ---------------------------------------------------------------------------
# Matches any SQL statement that modifies data, schema, or transaction state.
# READ-only statements (SELECT, PRAGMA without '=', EXPLAIN, COMMIT, ROLLBACK)
# are not matched so that in-flight reads continue unblocked during drain.
#
# Design rationale:
# - We match on the *first* non-comment keyword so cached prepared statements
#   cannot bypass the check (unlike set_authorizer, which is bypassed by the
#   SQLite statement cache for re-used statements).
# - Positive-match for PRAGMA <name> = or PRAGMA <name>( only — bare PRAGMA
#   reads are safe and must not be blocked.
_WRITE_SQL_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*"
    r"(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|BEGIN|VACUUM|REINDEX"
    r"|ATTACH|DETACH|PRAGMA\s+[a-zA-Z0-9_]+\s*(?:=|\())\b",
    re.IGNORECASE | re.DOTALL,
)


def _is_write_statement(sql: str) -> bool:
    """Return True if *sql* is a statement that writes data, schema, or opens a transaction.

    Used by CoordinatedCursor / CoordinatedConnection to enforce the drain gate
    so that no writes slip through while a key rotation is draining connections.
    """
    if not isinstance(sql, str):
        return False
    return bool(_WRITE_SQL_PATTERN.match(sql))


# ---------------------------------------------------------------------------
# Coordinated Connection & Cursor classes
# ---------------------------------------------------------------------------
# These subclasses intercept every write-capable SQL method and raise
# DatabaseDrainingError if the database is currently being drained for key
# rotation.  They are injected via the ``factory=`` parameter of
# sqlite3.connect / sqlcipher3.connect so that *every* connection returned by
# create_connection() automatically enforces the drain gate — no per-store
# patching is required.


class CoordinatedCursor(sqlite3.Cursor):
    """sqlite3.Cursor subclass that blocks write statements while draining."""

    def execute(self, sql: str, parameters: Any = (), /) -> "CoordinatedCursor":
        if _is_write_statement(sql):
            db_path = ConnectionCoordinator.get_connection_db_path(self.connection)
            if db_path and ConnectionCoordinator.is_draining(db_path):
                raise DatabaseDrainingError(
                    f"Database '{db_path}' is draining for key rotation; write rejected."
                )
        return super().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> "CoordinatedCursor":
        if _is_write_statement(sql):
            db_path = ConnectionCoordinator.get_connection_db_path(self.connection)
            if db_path and ConnectionCoordinator.is_draining(db_path):
                raise DatabaseDrainingError(
                    f"Database '{db_path}' is draining for key rotation; write rejected."
                )
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> "CoordinatedCursor":
        db_path = ConnectionCoordinator.get_connection_db_path(self.connection)
        if db_path and ConnectionCoordinator.is_draining(db_path):
            raise DatabaseDrainingError(
                f"Database '{db_path}' is draining for key rotation; script rejected."
            )
        return super().executescript(sql_script)


class CoordinatedConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that blocks write statements while draining."""

    def cursor(self, factory: Any = None) -> CoordinatedCursor:
        return super().cursor(factory or CoordinatedCursor)

    def execute(self, sql: str, parameters: Any = (), /) -> CoordinatedCursor:
        if _is_write_statement(sql):
            db_path = ConnectionCoordinator.get_connection_db_path(self)
            if db_path and ConnectionCoordinator.is_draining(db_path):
                raise DatabaseDrainingError(
                    f"Database '{db_path}' is draining for key rotation; write rejected."
                )
        return super().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> CoordinatedCursor:
        if _is_write_statement(sql):
            db_path = ConnectionCoordinator.get_connection_db_path(self)
            if db_path and ConnectionCoordinator.is_draining(db_path):
                raise DatabaseDrainingError(
                    f"Database '{db_path}' is draining for key rotation; write rejected."
                )
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> CoordinatedCursor:
        db_path = ConnectionCoordinator.get_connection_db_path(self)
        if db_path and ConnectionCoordinator.is_draining(db_path):
            raise DatabaseDrainingError(
                f"Database '{db_path}' is draining for key rotation; script rejected."
            )
        return super().executescript(sql_script)


if SQLCIPHER_AVAILABLE:
    class CoordinatedSQLCipherCursor(sqlcipher_dbapi2.Cursor):  # type: ignore[misc]
        """sqlcipher3.Cursor subclass that blocks write statements while draining."""

        def execute(self, sql: str, parameters: Any = (), /) -> "CoordinatedSQLCipherCursor":
            if _is_write_statement(sql):
                db_path = ConnectionCoordinator.get_connection_db_path(self.connection)
                if db_path and ConnectionCoordinator.is_draining(db_path):
                    raise DatabaseDrainingError(
                        f"Database '{db_path}' is draining for key rotation; write rejected."
                    )
            return super().execute(sql, parameters)

        def executemany(self, sql: str, seq_of_parameters: Any, /) -> "CoordinatedSQLCipherCursor":
            if _is_write_statement(sql):
                db_path = ConnectionCoordinator.get_connection_db_path(self.connection)
                if db_path and ConnectionCoordinator.is_draining(db_path):
                    raise DatabaseDrainingError(
                        f"Database '{db_path}' is draining for key rotation; write rejected."
                    )
            return super().executemany(sql, seq_of_parameters)

        def executescript(self, sql_script: str, /) -> "CoordinatedSQLCipherCursor":
            db_path = ConnectionCoordinator.get_connection_db_path(self.connection)
            if db_path and ConnectionCoordinator.is_draining(db_path):
                raise DatabaseDrainingError(
                    f"Database '{db_path}' is draining for key rotation; script rejected."
                )
            return super().executescript(sql_script)

    class CoordinatedSQLCipherConnection(sqlcipher_dbapi2.Connection):  # type: ignore[misc]
        """sqlcipher3.Connection subclass that blocks write statements while draining."""

        def cursor(self, factory: Any = None) -> "CoordinatedSQLCipherCursor":
            return super().cursor(factory or CoordinatedSQLCipherCursor)

        def execute(self, sql: str, parameters: Any = (), /) -> "CoordinatedSQLCipherCursor":
            if _is_write_statement(sql):
                db_path = ConnectionCoordinator.get_connection_db_path(self)
                if db_path and ConnectionCoordinator.is_draining(db_path):
                    raise DatabaseDrainingError(
                        f"Database '{db_path}' is draining for key rotation; write rejected."
                    )
            return super().execute(sql, parameters)

        def executemany(self, sql: str, seq_of_parameters: Any, /) -> "CoordinatedSQLCipherCursor":
            if _is_write_statement(sql):
                db_path = ConnectionCoordinator.get_connection_db_path(self)
                if db_path and ConnectionCoordinator.is_draining(db_path):
                    raise DatabaseDrainingError(
                        f"Database '{db_path}' is draining for key rotation; write rejected."
                    )
            return super().executemany(sql, seq_of_parameters)

        def executescript(self, sql_script: str, /) -> "CoordinatedSQLCipherCursor":
            db_path = ConnectionCoordinator.get_connection_db_path(self)
            if db_path and ConnectionCoordinator.is_draining(db_path):
                raise DatabaseDrainingError(
                    f"Database '{db_path}' is draining for key rotation; script rejected."
                )
            return super().executescript(sql_script)
else:
    # Fallback stubs when sqlcipher3 is not available so code that references
    # these names at import time does not raise NameError.
    CoordinatedSQLCipherCursor = None  # type: ignore[assignment,misc]
    CoordinatedSQLCipherConnection = None  # type: ignore[assignment,misc]


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


def is_database_encrypted(db_path: Path | str) -> bool:
    """Check if an on-disk database file is encrypted (i.e. lacks standard SQLite header).

    A standard plaintext SQLite database always begins with the 16-byte magic header:
    b"SQLite format 3\\x00". If the file does not exist or has 0 bytes, returns False.
    """
    path = Path(db_path)
    if not path.is_file():
        return False
    try:
        header = path.read_bytes()[:16]
        if len(header) < 16:
            return False
        return not header.startswith(b"SQLite format 3\x00")
    except Exception:
        return False


def can_acquire_exclusive_file_access(file_path: Path | str) -> bool:
    """Check if the current process can acquire exclusive file access without sharing.

    On Windows, uses kernel32.CreateFileW with dwShareMode=0 (FILE_SHARE_NONE).
    If another process or thread holds an open handle, CreateFileW returns INVALID_HANDLE_VALUE
    with GetLastError() == ERROR_SHARING_VIOLATION (32).
    """
    path = Path(file_path)
    if not path.is_file():
        return True

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            FILE_SHARE_NONE = 0
            OPEN_EXISTING = 3
            FILE_ATTRIBUTE_NORMAL = 0x80

            handle = kernel32.CreateFileW(
                str(path),
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_NONE,
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle == -1 or handle == 0xFFFFFFFF:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False

    # POSIX fallback
    try:
        import fcntl

        with open(path, "r+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return True
            except (BlockingIOError, OSError):
                return False
    except Exception:
        return True


class ConnectionCoordinator:
    """Coordinates active database connections and draining for maintenance and key rotation.

    Protects against Windows file-replacement contention and write-loss during rotation:
    1. Tracks all in-process connections and their active transaction status.
    2. Writes an on-disk advisory `<db>.drain` file so cross-process connections back off.
    3. Closes idle connections and allows active transactions to finish gracefully within a timeout.
    4. Prohibits new connections and new transactions while draining is active.
    5. Validates exclusive file access before returning from drain().
    6. Reliably resumes connection admissions upon rotation completion or safe abort.
    """

    _lock = threading.RLock()
    _connections: Dict[str, Dict[int, Any]] = {}
    _draining_events: Dict[str, threading.Event] = {}
    _marked_for_drain: set[int] = set()
    _conn_paths: Dict[int, str] = {}
    _conn_in_tx: set[int] = set()

    @classmethod
    def get_drain_path(cls, db_path: Path | str) -> Path:
        p = Path(db_path)
        return p.with_name(f"{p.name}.drain")

    @classmethod
    def _purge_closed_connections_locked(cls, path_str: str) -> None:
        if path_str not in cls._connections:
            return
        conns_map = cls._connections[path_str]
        to_purge = [
            cid for cid, conn in conns_map.items()
            if not cls._is_connection_open(conn)
        ]
        for cid in to_purge:
            conns_map.pop(cid, None)
            cls._conn_paths.pop(cid, None)
            cls._conn_in_tx.discard(cid)
            cls._marked_for_drain.discard(cid)

    @classmethod
    def register_connection(cls, db_path: Path | str, conn: Any) -> None:
        path_str = str(Path(db_path).resolve())
        conn_id = id(conn)
        with cls._lock:
            cls._purge_closed_connections_locked(path_str)
            cls._conn_paths[conn_id] = path_str
            if path_str not in cls._connections:
                cls._connections[path_str] = {}
            cls._connections[path_str][conn_id] = conn

    @classmethod
    def unregister_connection(cls, conn: Any) -> None:
        conn_id = id(conn)
        with cls._lock:
            cls._marked_for_drain.discard(conn_id)
            cls._conn_in_tx.discard(conn_id)
            path_str = cls._conn_paths.pop(conn_id, None)
            if path_str and path_str in cls._connections:
                cls._connections[path_str].pop(conn_id, None)

    @classmethod
    def get_connection_db_path(cls, conn: Any) -> Optional[str]:
        with cls._lock:
            return cls._conn_paths.get(id(conn))

    @classmethod
    def set_in_transaction(cls, conn: Any, in_tx: bool) -> None:
        conn_id = id(conn)
        with cls._lock:
            if in_tx:
                cls._conn_in_tx.add(conn_id)
            else:
                cls._conn_in_tx.discard(conn_id)

    @classmethod
    def mark_for_drain(cls, conn: Any) -> None:
        with cls._lock:
            cls._marked_for_drain.add(id(conn))

    @classmethod
    def is_marked_for_drain(cls, conn: Any) -> bool:
        with cls._lock:
            return id(conn) in cls._marked_for_drain

    @classmethod
    def _is_connection_open(cls, conn: Any) -> bool:
        try:
            _ = conn.in_transaction
            return True
        except Exception:
            return False

    @classmethod
    def _is_connection_in_transaction(cls, conn: Any) -> bool:
        try:
            conn_id = id(conn)
            with cls._lock:
                if conn_id in cls._conn_in_tx:
                    return True
            return bool(conn.in_transaction)
        except Exception:
            return False

    @classmethod
    def _is_drain_file_stale(cls, drain_file: Path) -> bool:
        try:
            stat = drain_file.stat()
            if time.time() - stat.st_mtime > 300:
                return True
            data = json.loads(drain_file.read_text(encoding="utf-8"))
            pid = data.get("coordinator_pid")
            if pid and isinstance(pid, int):
                from src.security.database_key_rotation import ExclusiveDatabaseLock

                return not ExclusiveDatabaseLock._is_pid_alive(pid)
        except Exception:
            pass
        return False

    @classmethod
    def is_draining(cls, db_path: Path | str) -> bool:
        p = Path(db_path).resolve()
        path_str = str(p)
        with cls._lock:
            if path_str in cls._draining_events:
                return True
        drain_file = cls.get_drain_path(p)
        if drain_file.is_file():
            if cls._is_drain_file_stale(drain_file):
                try:
                    drain_file.unlink()
                except Exception:
                    pass
                return False
            return True
        return False

    @classmethod
    def wait_for_drain(cls, db_path: Path | str, timeout: float = 10.0) -> bool:
        p = Path(db_path).resolve()
        path_str = str(p)
        start = time.time()
        while time.time() - start < timeout:
            with cls._lock:
                evt = cls._draining_events.get(path_str)
            if evt is not None:
                rem = max(0.05, timeout - (time.time() - start))
                evt.wait(timeout=min(0.2, rem))
            drain_file = cls.get_drain_path(p)
            if not drain_file.is_file() and (evt is None or evt.is_set()):
                return True
            time.sleep(0.05)
        return not cls.is_draining(p)

    @classmethod
    def set_drain(cls, db_path: Path | str, timeout_sec: float = 15.0) -> None:
        """Mark a database as draining across threads and processes.

        The drain file is written atomically via a sibling temp-file + os.replace
        so cross-process readers never observe a partial JSON document.
        (SEC-044-07 fix)
        """
        p = Path(db_path).resolve()
        path_str = str(p)
        drain_file = cls.get_drain_path(p)
        drain_file.parent.mkdir(parents=True, exist_ok=True)
        drain_payload = {
            "state": "DRAINING",
            "db_path": path_str,
            "coordinator_pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "timeout_sec": timeout_sec,
        }
        # Write to a temp sibling then atomically rename so readers always see
        # either a complete JSON document or the file not yet present.
        tmp_drain = drain_file.with_name(f"{drain_file.name}.{uuid4().hex[:8]}.tmp")
        try:
            tmp_drain.write_text(json.dumps(drain_payload, indent=2), encoding="utf-8")
            os.replace(tmp_drain, drain_file)
        except Exception:
            try:
                tmp_drain.unlink()
            except Exception:
                pass
            raise
        with cls._lock:
            cls._draining_events[path_str] = threading.Event()

    @classmethod
    def drain(
        cls,
        db_path: Path | str,
        timeout_sec: float = 15.0,
    ) -> None:
        p = Path(db_path).resolve()
        path_str = str(p)

        # 1. Write on-disk drain file and set in-process drain event
        cls.set_drain(p, timeout_sec=timeout_sec)

        start_time = time.time()

        # 3. Close idle connections and mark active ones
        with cls._lock:
            cls._purge_closed_connections_locked(path_str)
            conns = list(cls._connections.get(path_str, {}).values())
            for conn in conns:
                if not cls._is_connection_open(conn):
                    continue
                if cls._is_connection_in_transaction(conn):
                    cls._marked_for_drain.add(id(conn))
                else:
                    try:
                        conn.close()
                    except Exception:
                        pass
            cls._purge_closed_connections_locked(path_str)

        # 4. Wait for active transactions to complete gracefully and verify exclusive access
        while time.time() - start_time < timeout_sec:
            all_closed = True
            with cls._lock:
                cls._purge_closed_connections_locked(path_str)
                conns = list(cls._connections.get(path_str, {}).values())
                for conn in conns:
                    if cls._is_connection_open(conn):
                        if not cls._is_connection_in_transaction(conn):
                            try:
                                conn.close()
                            except Exception:
                                pass
                        else:
                            all_closed = False
                cls._purge_closed_connections_locked(path_str)

            if all_closed:
                if can_acquire_exclusive_file_access(p):
                    wal_file = p.with_name(f"{p.name}-wal")
                    if not wal_file.is_file() or can_acquire_exclusive_file_access(wal_file):
                        return

            time.sleep(0.05)

        # Timeout reached: release drain before raising
        cls.release_drain(p)
        from src.security.database_key_rotation import RotationDrainTimeoutError

        raise RotationDrainTimeoutError(
            f"Timed out after {timeout_sec}s waiting for database connections on '{p}' to drain."
        )

    @classmethod
    def release_drain(cls, db_path: Path | str) -> None:
        p = Path(db_path).resolve()
        path_str = str(p)
        with cls._lock:
            evt = cls._draining_events.pop(path_str, None)
            if evt is not None:
                evt.set()
        drain_file = cls.get_drain_path(p)
        if drain_file.is_file():
            try:
                drain_file.unlink()
            except Exception:
                pass


def create_connection(
    db_path: Path | str,
    pragma_config: Optional[SQLitePragmaConfig] = None,
    timeout_sec: float = 5.0,
    encryption_key: Optional[bytes] = None,
    encrypted: Optional[bool] = None,
    drain_wait_timeout: float = 10.0,
) -> SQLiteConnection:
    """Initialize and configure a robust, scoped SQLite/SQLCipher connection.

    Uses isolation_level=None (autocommit mode) so that explicit transaction
    statements (BEGIN, COMMIT, ROLLBACK) are handled with deterministic precision.

    Args:
        db_path: Target filesystem path or :memory:.
        pragma_config: SQLite runtime configuration directives.
        timeout_sec: Busy lock retry timeout.
        encryption_key: Raw 32-byte cryptographic key. If provided, SQLCipher is required.
        encrypted: Explicit boolean flag. If True, encryption is strictly enforced and will
            fail-closed if keys are missing or SQLCipher is unavailable. If None, encryption
            is auto-detected (enabled if encryption_key is provided or DB is encrypted).
        drain_wait_timeout: Seconds to wait if database is currently draining for key rotation.

    Raises:
        DatabaseConnectionError: If connection, key initialization, or PRAGMA verification fails.
        DatabaseLockedError: If database file is locked during connection.
        DatabaseDrainingError: If database is currently draining for key rotation.
    """
    cfg = pragma_config or SQLitePragmaConfig()
    path_str = str(db_path)
    is_memory = path_str == ":memory:" or path_str.startswith("file::memory:")
    validated_path: Optional[Path] = None

    if not is_memory:
        from src.storage.paths import validate_database_path

        validated_path = validate_database_path(db_path).resolve()
        path_str = str(validated_path)
        if ConnectionCoordinator.is_draining(validated_path):
            if drain_wait_timeout > 0:
                drained = ConnectionCoordinator.wait_for_drain(
                    validated_path, timeout=drain_wait_timeout
                )
                if not drained:
                    raise DatabaseDrainingError(
                        f"Database '{validated_path}' is currently draining for key rotation; "
                        f"connection blocked."
                    )
            else:
                raise DatabaseDrainingError(
                    f"Database '{validated_path}' is currently draining for key rotation."
                )

    # Determine encryption enforcement
    require_encryption = False
    if encrypted is True:
        require_encryption = True
    elif encrypted is False:
        require_encryption = False
    else:
        # Auto-detect: required if explicit key supplied or existing file is encrypted
        if encryption_key is not None:
            require_encryption = True
        elif not is_memory and Path(path_str).is_file() and is_database_encrypted(path_str):
            require_encryption = True

    if require_encryption:
        if not SQLCIPHER_AVAILABLE:
            raise DatabaseConnectionError(
                "Database encryption requested or required, but sqlcipher3 is not available."
            )

        # Resolve encryption key if not provided
        active_key = encryption_key
        if active_key is None:
            from src.security.database_key import DatabaseKeyManager

            key_mgr = DatabaseKeyManager()
            active_key = key_mgr.get_key()
            if active_key is None:
                raise DatabaseConnectionError(
                    f"Encrypted access required for '{path_str}', but master key is missing."
                )

        if len(active_key) != 32:
            raise DatabaseConnectionError(
                f"Invalid encryption key length: {len(active_key)} bytes (expected 32 bytes)."
            )

        try:
            conn = sqlcipher3.connect(
                path_str,
                timeout=timeout_sec,
                isolation_level=None,
                factory=CoordinatedSQLCipherConnection if CoordinatedSQLCipherConnection is not None else sqlcipher3.Connection,
            )
            conn.row_factory = sqlcipher3.Row

            # Apply PRAGMA key immediately before any database operation
            hex_key = active_key.hex()
            # SQLCipher raw key syntax: PRAGMA key = "x'HEX'";
            key_pragma = f"PRAGMA key = \"x'{hex_key}'\";"
            conn.execute(key_pragma)

            # Fail-closed authentication check: verify that key can read sqlite_master
            try:
                conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
            except Exception as auth_exc:
                conn.close()
                raise DatabaseConnectionError(
                    f"Failed to authenticate/decrypt database '{path_str}': {auth_exc}"
                ) from auth_exc

        except Exception as exc:
            if isinstance(exc, (DatabaseConnectionError, DatabaseLockedError)):
                raise
            raise DatabaseConnectionError(
                f"Failed to establish SQLCipher connection to '{path_str}': {exc}"
            ) from exc

    else:
        # Standard unencrypted SQLite connection
        try:
            conn = sqlite3.connect(
                path_str,
                timeout=timeout_sec,
                isolation_level=None,
                factory=CoordinatedConnection,
            )
            conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                raise DatabaseLockedError(
                    f"Database lock contention on '{path_str}': {exc}"
                ) from exc
            raise DatabaseConnectionError(
                f"Failed to establish SQLite connection to '{path_str}': {exc}"
            ) from exc
        except Exception as exc:
            raise DatabaseConnectionError(
                f"Unexpected error connecting to SQLite database at '{path_str}': {exc}"
            ) from exc

    # Configure common PRAGMAs
    try:
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

        # 4. Journal Mode (WAL on disk, MEMORY on :memory:)
        target_journal = "MEMORY" if is_memory else cfg.journal_mode
        actual_journal = conn.execute(f"PRAGMA journal_mode = {target_journal};").fetchone()[0]
        if not is_memory and cfg.journal_mode.upper() == "WAL":
            if str(actual_journal).upper() != "WAL":
                raise DatabaseConnectionError(
                    f"Failed to set WAL journal mode. Effective mode: '{actual_journal}'"
                )

        # 5. Synchronous Mode
        conn.execute(f"PRAGMA synchronous = {cfg.synchronous};")

        if validated_path is not None:
            ConnectionCoordinator.register_connection(validated_path, conn)

        return conn

    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        if isinstance(exc, (DatabaseConnectionError, DatabaseLockedError)):
            raise
        raise DatabaseConnectionError(
            f"Failed to configure PRAGMAs on '{path_str}': {exc}"
        ) from exc


@contextmanager
def open_database(
    db_path: Path | str,
    pragma_config: Optional[SQLitePragmaConfig] = None,
    timeout_sec: float = 5.0,
    encryption_key: Optional[bytes] = None,
    encrypted: Optional[bool] = None,
    drain_wait_timeout: float = 10.0,
) -> Generator[SQLiteConnection, None, None]:
    """Context manager for scoped SQLite / SQLCipher connections.

    Guarantees clean shutdown and closing upon scope exit.
    """
    conn = create_connection(
        db_path,
        pragma_config=pragma_config,
        timeout_sec=timeout_sec,
        encryption_key=encryption_key,
        encrypted=encrypted,
        drain_wait_timeout=drain_wait_timeout,
    )
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
        ConnectionCoordinator.unregister_connection(conn)


@contextmanager
def transaction(
    conn: SQLiteConnection,
    immediate: bool = True,
) -> Generator[sqlite3.Cursor, None, None]:
    """Deterministic transaction context manager.

    Behaviors:
    - Rejects new transactions if database is currently draining for key rotation.
    - Rejects illegal nested transactions with TransactionError.
    - Emits BEGIN IMMEDIATE (default) or BEGIN.
    - Commits on normal block exit.
    - Rolls back on unhandled exception and preserves original exception.
    - Closes connection upon transaction exit if marked for draining.
    """
    db_path = ConnectionCoordinator.get_connection_db_path(conn)
    if db_path and ConnectionCoordinator.is_draining(db_path):
        raise DatabaseDrainingError(
            f"Database '{db_path}' is draining for key rotation; new transactions are rejected."
        )

    if conn.in_transaction or ConnectionCoordinator._is_connection_in_transaction(conn):
        raise TransactionError(
            "Nested transaction rejected: A transaction is already active on this connection. "
            "Use sequential transaction boundaries or explicit savepoints."
        )

    ConnectionCoordinator.set_in_transaction(conn, True)
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
        ConnectionCoordinator.set_in_transaction(conn, False)
        cursor.close()
        if ConnectionCoordinator.is_marked_for_drain(conn):
            try:
                conn.close()
            except Exception:
                pass
            ConnectionCoordinator.unregister_connection(conn)


def assert_not_draining(conn: SQLiteConnection) -> None:
    """Raise DatabaseDrainingError if the database backing *conn* is currently draining.

    Store classes that bypass the ``transaction()`` context manager and issue
    ``BEGIN IMMEDIATE`` directly MUST call this at the start of every write
    method so that the key-rotation drain gate is uniformly enforced.
    (SEC-044-03 fix)
    """
    db_path = ConnectionCoordinator.get_connection_db_path(conn)
    if db_path and ConnectionCoordinator.is_draining(db_path):
        raise DatabaseDrainingError(
            f"Database '{db_path}' is draining for key rotation; write rejected."
        )


def migrate_database_to_encrypted(
    source_path: Path | str,
    encryption_key: bytes,
    backup_dir: Optional[Path | str] = None,
) -> Path:
    """Safely migrate an existing plaintext SQLite database to an encrypted SQLCipher database.

    Procedure:
    1. Verify SQLCipher is available and source file exists.
    2. Check if source is already encrypted; if so, returns immediately.
    3. Checkpoint WAL frames to establish snapshot consistency.
    4. Create, cryptographically verify, and atomically publish an encrypted SQLCipher backup.
    5. Export source into an encrypted temporary staging file.
    6. Verify cryptographic and relational integrity of the staging file (PRAGMA integrity_check).
    7. Atomically replace the plaintext database with the encrypted file via os.replace.
    8. Authenticate and verify the newly replaced database.
    9. Clean up residual plaintext WAL/SHM files.

    Returns:
        Path to the migrated encrypted database.

    Raises:
        DatabaseConnectionError: If migration or verification fails.
    """
    if not SQLCIPHER_AVAILABLE:
        raise DatabaseConnectionError(
            "Cannot migrate to encrypted database: sqlcipher3 is not available."
        )

    src = Path(source_path).resolve()
    if not src.is_file():
        raise DatabaseConnectionError(f"Source database file does not exist: '{src}'")

    if is_database_encrypted(src):
        logger.info("Database at '%s' is already encrypted; migration skipped.", src)
        return src

    if len(encryption_key) != 32:
        raise DatabaseConnectionError("Encryption key must be exactly 32 bytes.")

    hex_key = encryption_key.hex()

    # Step 0: Ensure WAL consistency by checkpointing committed transactions into main file
    try:
        chk_conn = sqlite3.connect(str(src))
        chk_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        chk_conn.close()
    except Exception as wal_exc:
        logger.warning("Could not execute pre-backup wal_checkpoint on '%s': %s", src, wal_exc)

    # Step 1: Create, verify, and publish encrypted pre-migration backup snapshot
    b_dir = Path(backup_dir) if backup_dir else src.parent / "backups"
    try:
        b_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise DatabaseConnectionError(f"Failed to initialize backup directory: {exc}") from exc
    backup_path = b_dir / f"{src.stem}_pre_encryption_backup.db"
    backup_staging = b_dir / f"{src.stem}_backup_{uuid4().hex[:8]}.staging"

    if backup_staging.exists():
        try:
            backup_staging.unlink()
        except Exception:
            pass

    try:
        plain_conn = sqlcipher3.connect(str(src))
        plain_conn.execute(
            f"ATTACH DATABASE '{backup_staging}' AS enc_backup KEY \"x'{hex_key}'\";"
        )
        plain_conn.execute("SELECT sqlcipher_export('enc_backup');")
        plain_conn.execute("DETACH DATABASE enc_backup;")
        plain_conn.close()
    except Exception as exc:
        if backup_staging.exists():
            try:
                backup_staging.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(
            f"Failed to create encrypted pre-migration backup snapshot: {exc}"
        ) from exc

    # Cryptographic integrity verification of the backup snapshot before publication
    try:
        v_conn = sqlcipher3.connect(str(backup_staging))
        v_conn.execute(f"PRAGMA key = \"x'{hex_key}'\";")
        chk_row = v_conn.execute("PRAGMA integrity_check;").fetchone()
        v_conn.close()

        if chk_row is None or str(chk_row[0]).lower() != "ok":
            if backup_staging.exists():
                backup_staging.unlink()
            raise DatabaseConnectionError(
                f"Integrity check failed on encrypted staging backup: {chk_row}"
            )

        if not is_database_encrypted(backup_staging):
            if backup_staging.exists():
                backup_staging.unlink()
            raise DatabaseConnectionError(
                "Staging backup failed encryption verification (unencrypted header)."
            )
    except Exception as exc:
        if backup_staging.exists():
            try:
                backup_staging.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(
            f"Cryptographic verification of backup failed: {exc}"
        ) from exc

    # Atomically publish verified encrypted backup
    try:
        os.replace(backup_staging, backup_path)
        logger.info("Created verified encrypted pre-migration backup at '%s'", backup_path)
    except Exception as exc:
        if backup_staging.exists():
            try:
                backup_staging.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(
            f"Failed to publish verified encrypted backup: {exc}"
        ) from exc

    # Step 2: Export via sqlcipher_export into temporary staging file for source replacement
    staging_file = src.parent / f"{src.name}.encrypted.staging"
    if staging_file.exists():
        try:
            staging_file.unlink()
        except Exception:
            pass

    try:
        plain_conn = sqlcipher3.connect(str(src))
        plain_conn.execute(f"ATTACH DATABASE '{staging_file}' AS encrypted KEY \"x'{hex_key}'\";")
        plain_conn.execute("SELECT sqlcipher_export('encrypted');")
        plain_conn.execute("DETACH DATABASE encrypted;")
        plain_conn.close()
    except Exception as exc:
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(
            f"sqlcipher_export failed during encryption migration: {exc}"
        ) from exc

    # Step 3: Integrity verification of staging database
    try:
        verify_conn = sqlcipher3.connect(str(staging_file))
        verify_conn.execute(f"PRAGMA key = \"x'{hex_key}'\";")
        check_row = verify_conn.execute("PRAGMA integrity_check;").fetchone()
        verify_conn.close()

        if check_row is None or str(check_row[0]).lower() != "ok":
            if staging_file.exists():
                staging_file.unlink()
            raise DatabaseConnectionError(
                f"Integrity check failed on encrypted staging database: {check_row}"
            )
    except Exception as exc:
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(
            f"Integrity verification failed for encrypted database: {exc}"
        ) from exc

    # Step 4: Atomic replacement of source file
    try:
        os.replace(staging_file, src)
        logger.info(
            "Successfully replaced plaintext database '%s' with encrypted SQLCipher database.",
            src,
        )
    except Exception as exc:
        raise DatabaseConnectionError(
            f"Atomic replacement failed during encryption migration: {exc}"
        ) from exc

    # Step 5: Post-replacement verification (confirm source opens and authenticates)
    try:
        post_conn = sqlcipher3.connect(str(src))
        post_conn.execute(f"PRAGMA key = \"x'{hex_key}'\";")
        post_conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
        post_conn.close()
    except Exception as exc:
        raise DatabaseConnectionError(
            f"Post-migration authentication verification failed for '{src}': {exc}"
        ) from exc

    # Step 6: Clean up associated plaintext WAL / SHM files if present
    wal_file = src.with_name(f"{src.name}-wal")
    shm_file = src.with_name(f"{src.name}-shm")
    for f in (wal_file, shm_file):
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass

    return src


def restore_database_from_backup(
    backup_path: Path | str,
    target_path: Path | str,
    encryption_key: bytes,
    as_plaintext: bool = False,
) -> Path:
    """Restore a database from a verified encrypted SQLCipher backup snapshot.

    Args:
        backup_path: Path to the encrypted backup database file.
        target_path: Destination path for the restored database.
        encryption_key: 32-byte master encryption key for the backup.
        as_plaintext: If True, exports the backup as a standard unencrypted SQLite DB.
                      If False (default), restores as an encrypted SQLCipher DB.

    Returns:
        Path to the restored database file.

    Raises:
        DatabaseConnectionError: If restore or verification fails.
    """
    if not SQLCIPHER_AVAILABLE:
        raise DatabaseConnectionError("Cannot restore database: sqlcipher3 is not available.")

    b_path = Path(backup_path).resolve()
    if not b_path.is_file():
        raise DatabaseConnectionError(f"Backup file does not exist: '{b_path}'")

    if len(encryption_key) != 32:
        raise DatabaseConnectionError("Encryption key must be exactly 32 bytes.")

    tgt = Path(target_path).resolve()
    tgt.parent.mkdir(parents=True, exist_ok=True)
    hex_key = encryption_key.hex()

    staging_file = tgt.parent / f"{tgt.name}.restore_{uuid4().hex[:8]}.staging"
    if staging_file.exists():
        try:
            staging_file.unlink()
        except Exception:
            pass

    try:
        b_conn = sqlcipher3.connect(str(b_path))
        b_conn.execute(f"PRAGMA key = \"x'{hex_key}'\";")
        b_conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()

        if as_plaintext:
            b_conn.execute(f"ATTACH DATABASE '{staging_file}' AS plaintext KEY '';")
            b_conn.execute("SELECT sqlcipher_export('plaintext');")
            b_conn.execute("DETACH DATABASE plaintext;")
        else:
            b_conn.execute(f"ATTACH DATABASE '{staging_file}' AS enc_target KEY \"x'{hex_key}'\";")
            b_conn.execute("SELECT sqlcipher_export('enc_target');")
            b_conn.execute("DETACH DATABASE enc_target;")
        b_conn.close()
    except Exception as exc:
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(f"Failed during backup export: {exc}") from exc

    try:
        if as_plaintext:
            chk_conn = sqlite3.connect(str(staging_file))
            chk = chk_conn.execute("PRAGMA integrity_check;").fetchone()
            chk_conn.close()
        else:
            chk_conn = sqlcipher3.connect(str(staging_file))
            chk_conn.execute(f"PRAGMA key = \"x'{hex_key}'\";")
            chk = chk_conn.execute("PRAGMA integrity_check;").fetchone()
            chk_conn.close()

        if chk is None or str(chk[0]).lower() != "ok":
            if staging_file.exists():
                staging_file.unlink()
            raise DatabaseConnectionError(f"Integrity check failed on restored staging: {chk}")
    except Exception as exc:
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(
            f"Restored database integrity verification failed: {exc}"
        ) from exc

    try:
        os.replace(staging_file, tgt)
        logger.info("Successfully restored database from '%s' to '%s'", b_path, tgt)
    except Exception as exc:
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(f"Failed to publish restored database: {exc}") from exc

    return tgt


def scan_legacy_plaintext_backups(
    backup_dir: Optional[Path | str] = None,
) -> List[Dict[str, Any]]:
    """Scan backup directory for legacy plaintext SQLite backups without reading contents.

    Returns:
        List of dictionaries with metadata for each detected backup file:
        path, name, size_bytes, is_plaintext, status ('PLAINTEXT_EXPOSED' or 'ENCRYPTED').
    """
    from src.storage.paths import get_default_backups_dir

    b_dir = Path(backup_dir) if backup_dir else get_default_backups_dir()
    results: List[Dict[str, Any]] = []

    if not b_dir.is_dir():
        return results

    for item in b_dir.iterdir():
        if item.is_file():
            try:
                raw_bytes = item.read_bytes()[:16]
                is_plain = raw_bytes.startswith(b"SQLite format 3\x00")
                results.append(
                    {
                        "path": str(item),
                        "name": item.name,
                        "size_bytes": item.stat().st_size,
                        "is_plaintext": is_plain,
                        "status": "PLAINTEXT_EXPOSED" if is_plain else "ENCRYPTED",
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "path": str(item),
                        "name": item.name,
                        "size_bytes": item.stat().st_size if item.exists() else 0,
                        "is_plaintext": False,
                        "status": f"UNREADABLE ({exc})",
                    }
                )

    return results


def remediate_legacy_backup(
    backup_path: Path | str,
    encryption_key: bytes,
    dry_run: bool = True,
    remove_plaintext: bool = False,
) -> Dict[str, Any]:
    """Safely remediate a legacy plaintext backup file by encrypting it.

    In dry_run mode (default), performs zero filesystem modifications.
    When dry_run=False, creates and verifies an encrypted copy before optional removal.
    """
    path = Path(backup_path).resolve()
    if not path.is_file():
        raise DatabaseConnectionError(f"Legacy backup file does not exist: '{path}'")

    if len(encryption_key) != 32:
        raise DatabaseConnectionError("Encryption key must be exactly 32 bytes.")

    is_plain = not is_database_encrypted(path)
    if not is_plain:
        return {
            "path": str(path),
            "action": "SKIPPED",
            "message": "File is already encrypted or not a valid SQLite database.",
        }

    enc_target = path.with_name(f"{path.stem}.enc.db")
    if dry_run:
        return {
            "path": str(path),
            "action": "DRY_RUN",
            "target_path": str(enc_target),
            "would_encrypt": True,
            "would_remove_plaintext": remove_plaintext,
            "message": "Dry-run only: no files modified or deleted.",
        }

    if not SQLCIPHER_AVAILABLE:
        raise DatabaseConnectionError("sqlcipher3 is required to encrypt legacy backups.")

    hex_key = encryption_key.hex()
    staging_file = path.with_name(f"{path.stem}.remediate_{uuid4().hex[:8]}.staging")
    try:
        plain_conn = sqlcipher3.connect(str(path))
        plain_conn.execute(f"ATTACH DATABASE '{staging_file}' AS enc KEY \"x'{hex_key}'\";")
        plain_conn.execute("SELECT sqlcipher_export('enc');")
        plain_conn.execute("DETACH DATABASE enc;")
        plain_conn.close()

        v_conn = sqlcipher3.connect(str(staging_file))
        v_conn.execute(f"PRAGMA key = \"x'{hex_key}'\";")
        chk = v_conn.execute("PRAGMA integrity_check;").fetchone()
        v_conn.close()

        if chk is None or str(chk[0]).lower() != "ok":
            if staging_file.exists():
                staging_file.unlink()
            raise DatabaseConnectionError(f"Integrity check failed on remediated backup: {chk}")

        os.replace(staging_file, enc_target)

        removed = False
        if remove_plaintext:
            path.unlink()
            removed = True

        return {
            "path": str(path),
            "action": "REMEDIATED",
            "target_path": str(enc_target),
            "plaintext_removed": removed,
            "message": "Legacy backup successfully encrypted and verified.",
        }
    except Exception as exc:
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass
        raise DatabaseConnectionError(f"Failed to remediate legacy backup: {exc}") from exc


def initialize_runtime_database(
    db_path: Optional[Path | str] = None,
    migrations_dir: Optional[Path | str] = None,
    encryption_key: Optional[bytes] = None,
    encrypted: Optional[bool] = None,
) -> SQLiteConnection:
    """Initialize, migrate, and encrypt the canonical SAGE SQLite database.

    1. Resolves path to %USERPROFILE%\\.aegis\\data\\aegis.db if not specified.
    2. Validates path against strict storage boundaries (anti-OneDrive, anti-repo, anti-C:\\AEGIS).
    3. Ensures parent directory exists.
    4. If encryption is required and plaintext DB is present, safely migrates it.
    5. Establishes connection with WAL journal mode, NORMAL synchronous, foreign keys.
    6. Loads and applies all pending migrations monotonically.

    Returns:
        Active, fully migrated SQLiteConnection.
    """
    from src.security.database_key import DatabaseKeyManager
    from src.storage.migrations import apply_migrations, load_migrations_from_directory
    from src.storage.paths import get_default_db_path, validate_database_path

    target_path = Path(db_path) if db_path is not None else get_default_db_path()
    validated_path = validate_database_path(target_path)
    validated_path.parent.mkdir(parents=True, exist_ok=True)

    path_str = str(validated_path)
    is_memory = path_str == ":memory:" or path_str.startswith("file::memory:")

    # Determine encryption intent
    use_encryption = encrypted
    if use_encryption is None:
        # Default behavior: if sqlcipher is available and not in-memory test without explicit key,
        # enable encryption by default.
        if SQLCIPHER_AVAILABLE:
            use_encryption = True
        else:
            use_encryption = False

    active_key = encryption_key
    key_mgr: Optional[DatabaseKeyManager] = None
    if use_encryption and not is_memory:
        key_mgr = DatabaseKeyManager()
        if active_key is None:
            active_key = key_mgr.get_or_create_key()

        # Check for and recover any interrupted key rotation prior to connecting.
        # SEC-044-01 fix: acquire ExclusiveDatabaseLock before any recovery action so
        # that a concurrently booting second process cannot race to delete staging files
        # belonging to an ongoing rotation in another process.
        if validated_path.is_file():
            from src.security.database_key_rotation import (
                ExclusiveDatabaseLock,
                RotationLockError,
                recover_interrupted_rotation,
            )

            try:
                with ExclusiveDatabaseLock(validated_path, timeout_sec=10.0):
                    recovery_res = recover_interrupted_rotation(validated_path, key_manager=key_mgr)
                    if recovery_res.get("status") in ("ROLLED_FORWARD", "ROLLED_BACK"):
                        logger.info(
                            "Recovered interrupted key rotation on startup for '%s': %s",
                            validated_path,
                            recovery_res,
                        )
                        if encryption_key is None:
                            active_key = key_mgr.get_key()
            except RotationLockError:
                # Another process holds the rotation lock and is actively rotating;
                # skip recovery here — the lock-holder is responsible for completion.
                # Wait for drain to finish so we don't open with a stale pre-rotation key.
                logger.info(
                    "Skipping startup recovery for '%s': rotation lock held by another process. "
                    "Waiting for drain to complete before connecting.",
                    validated_path,
                )
                ConnectionCoordinator.wait_for_drain(validated_path, timeout=15.0)
                # Reload the key in case rotation completed and committed a new key
                if encryption_key is None:
                    active_key = key_mgr.get_key()

    # If file exists on disk and is plaintext, migrate it before connecting
    if use_encryption and active_key and not is_memory and validated_path.is_file():
        if not is_database_encrypted(validated_path):
            logger.info(
                "Existing plaintext database detected at '%s'; migrating to encrypted...",
                validated_path,
            )
            migrate_database_to_encrypted(validated_path, active_key)

    conn = create_connection(
        validated_path,
        encryption_key=active_key if use_encryption else None,
        encrypted=use_encryption,
    )

    # Resolve migrations directory
    if migrations_dir is not None:
        mig_dir = Path(migrations_dir)
    else:
        # Canonical repo root / migrations
        mig_dir = Path(__file__).resolve().parent.parent.parent / "migrations"

    if mig_dir.is_dir():
        migrations = load_migrations_from_directory(mig_dir)
        apply_migrations(conn, migrations, database_path=validated_path)

    return conn

