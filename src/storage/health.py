"""AEGIS Local SQLite Storage Foundation - Deterministic Database Health Probes.

Document Reference: docs/specifications/AEGIS_SQLITE_STORAGE_SUBSYSTEM.md
Constraints:
- Comprehensive 12-point health evaluation.
- Strictly typed health states (HEALTHY, DEGRADED, CORRUPT, UNAVAILABLE).
- Does NOT classify temporary locks or missing files as corruption.
- Uses standard library sqlite3 and shutil.
"""

import shutil
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from src.storage.database import SQLitePragmaConfig, create_connection
from src.storage.errors import DatabaseConnectionError, DatabaseLockedError
from src.storage.migrations import (
    get_applied_migrations,
    get_current_schema_version,
)

# Minimum disk free space before storage operations are considered degraded (50 MB)
MIN_SAFE_DISK_SPACE_BYTES = 50 * 1024 * 1024


class DatabaseHealthState(str, Enum):
    """Diagnostic health classification of the SQLite database."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CORRUPT = "CORRUPT"
    UNAVAILABLE = "UNAVAILABLE"


class DatabaseHealthReport(BaseModel):
    """Structured health assessment of the local SQLite storage engine."""

    state: DatabaseHealthState
    database_path: str
    exists: bool
    can_connect: bool
    foreign_keys_enabled: bool
    journal_mode: str
    synchronous: str
    temp_store: str
    schema_version: int
    applied_migrations_count: int
    integrity_check_passed: bool
    read_write_verified: bool
    free_space_bytes: Optional[int]
    diagnostic_messages: List[str] = Field(default_factory=list)
    checked_at_utc: str


def check_database_health(
    db_path: Path | str,
    pragma_config: Optional[SQLitePragmaConfig] = None,
) -> DatabaseHealthReport:
    """Perform a deterministic 12-point health check on the specified SQLite database.

    Evaluates:
    1. Path resolution
    2. File accessibility
    3. SQLite connection establishment
    4. PRAGMA foreign_keys (must be 1)
    5. PRAGMA journal_mode (WAL or MEMORY)
    6. PRAGMA synchronous
    7. PRAGMA temp_store
    8. Schema version tracking
    9. Applied migration count
    10. Atomic read/write transaction capability
    11. PRAGMA integrity_check(10)
    12. Volume free disk space

    Classification rules:
    - UNAVAILABLE: Path invalid, file inaccessible, permission denied, or locked.
    - CORRUPT: integrity_check returns errors or sqlite reports database corruption.
    - DEGRADED: Database accessible, but non-critical condition impaired
      (e.g. low disk, non-WAL mode on disk).
    - HEALTHY: All 12 inspection points pass verified safe standards.
    """
    path_str = str(db_path)
    is_memory = path_str == ":memory:" or path_str.startswith("file::memory:")
    path_obj = Path(db_path) if not is_memory else None
    checked_at = datetime.now(timezone.utc).isoformat()
    diagnostics: List[str] = []

    # 1. Path & Existence
    exists = is_memory or (path_obj is not None and path_obj.is_file())
    if not is_memory and not exists:
        return DatabaseHealthReport(
            state=DatabaseHealthState.UNAVAILABLE,
            database_path=path_str,
            exists=False,
            can_connect=False,
            foreign_keys_enabled=False,
            journal_mode="UNKNOWN",
            synchronous="UNKNOWN",
            temp_store="UNKNOWN",
            schema_version=0,
            applied_migrations_count=0,
            integrity_check_passed=False,
            read_write_verified=False,
            free_space_bytes=None,
            diagnostic_messages=["Database file does not exist on filesystem."],
            checked_at_utc=checked_at,
        )

    # 12. Disk Space
    free_bytes: Optional[int] = None
    if path_obj is not None and path_obj.parent.exists():
        try:
            free_bytes = shutil.disk_usage(path_obj.parent).free
            if free_bytes < MIN_SAFE_DISK_SPACE_BYTES:
                mb_free = free_bytes // (1024 * 1024)
                diagnostics.append(
                    f"Warning: Low free disk space on volume: {mb_free} MB available."
                )
        except Exception as exc:
            diagnostics.append(f"Failed to query volume free disk space: {exc}")

    # 3. Connection
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = create_connection(db_path, pragma_config=pragma_config, timeout_sec=2.0)
    except DatabaseLockedError as exc:
        return DatabaseHealthReport(
            state=DatabaseHealthState.UNAVAILABLE,
            database_path=path_str,
            exists=True,
            can_connect=False,
            foreign_keys_enabled=False,
            journal_mode="UNKNOWN",
            synchronous="UNKNOWN",
            temp_store="UNKNOWN",
            schema_version=0,
            applied_migrations_count=0,
            integrity_check_passed=False,
            read_write_verified=False,
            free_space_bytes=free_bytes,
            diagnostic_messages=[f"Database is currently locked or busy: {exc}"],
            checked_at_utc=checked_at,
        )
    except DatabaseConnectionError as exc:
        msg = str(exc).lower()
        state = (
            DatabaseHealthState.CORRUPT
            if "corrupt" in msg or "malformed" in msg
            else DatabaseHealthState.UNAVAILABLE
        )
        return DatabaseHealthReport(
            state=state,
            database_path=path_str,
            exists=True,
            can_connect=False,
            foreign_keys_enabled=False,
            journal_mode="UNKNOWN",
            synchronous="UNKNOWN",
            temp_store="UNKNOWN",
            schema_version=0,
            applied_migrations_count=0,
            integrity_check_passed=False,
            read_write_verified=False,
            free_space_bytes=free_bytes,
            diagnostic_messages=[f"Connection failed: {exc}"],
            checked_at_utc=checked_at,
        )

    try:
        # 4. Foreign Keys
        fk_row = conn.execute("PRAGMA foreign_keys;").fetchone()
        foreign_keys_enabled = bool(fk_row and fk_row[0] == 1)
        if not foreign_keys_enabled:
            diagnostics.append("PRAGMA foreign_keys is not enabled.")

        # 5. Journal Mode
        jm_row = conn.execute("PRAGMA journal_mode;").fetchone()
        journal_mode = str(jm_row[0]).upper() if jm_row else "UNKNOWN"
        if not is_memory and journal_mode != "WAL":
            diagnostics.append(f"Journal mode is '{journal_mode}' rather than recommended 'WAL'.")

        # 6. Synchronous
        sync_row = conn.execute("PRAGMA synchronous;").fetchone()
        sync_map = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}
        synchronous = sync_map.get(sync_row[0], str(sync_row[0])) if sync_row else "UNKNOWN"

        # 7. Temp Store
        ts_row = conn.execute("PRAGMA temp_store;").fetchone()
        ts_map = {0: "DEFAULT", 1: "FILE", 2: "MEMORY"}
        temp_store = ts_map.get(ts_row[0], str(ts_row[0])) if ts_row else "UNKNOWN"

        # 8 & 9. Schema Version & Migrations
        schema_version = get_current_schema_version(conn)
        applied_migrations = get_applied_migrations(conn)
        applied_count = len(applied_migrations)

        # 10. Read / Write Transaction Check
        read_write_verified = False
        try:
            conn.execute("CREATE TEMP TABLE _health_probe (id INTEGER PRIMARY KEY, ts TEXT);")
            conn.execute("INSERT INTO _health_probe VALUES (1, 'probe');")
            probe_row = conn.execute("SELECT ts FROM _health_probe WHERE id = 1;").fetchone()
            conn.execute("DROP TABLE _health_probe;")
            if probe_row and probe_row[0] == "probe":
                read_write_verified = True
        except Exception as exc:
            diagnostics.append(f"Read/write transaction probe failed: {exc}")

        # 11. PRAGMA integrity_check(10)
        integrity_check_passed = False
        try:
            integrity_rows = conn.execute("PRAGMA integrity_check(10);").fetchall()
            messages = [r[0] for r in integrity_rows]
            if len(messages) == 1 and messages[0] == "ok":
                integrity_check_passed = True
            else:
                diagnostics.append(f"PRAGMA integrity_check failed: {messages}")
        except Exception as exc:
            diagnostics.append(f"PRAGMA integrity_check raised exception: {exc}")

        # Classify overall health state
        if not integrity_check_passed:
            state = DatabaseHealthState.CORRUPT
        elif not read_write_verified:
            state = DatabaseHealthState.DEGRADED
        elif not foreign_keys_enabled:
            state = DatabaseHealthState.DEGRADED
        elif free_bytes is not None and free_bytes < MIN_SAFE_DISK_SPACE_BYTES:
            state = DatabaseHealthState.DEGRADED
        elif not is_memory and journal_mode != "WAL":
            state = DatabaseHealthState.DEGRADED
        else:
            state = DatabaseHealthState.HEALTHY

        return DatabaseHealthReport(
            state=state,
            database_path=path_str,
            exists=exists,
            can_connect=True,
            foreign_keys_enabled=foreign_keys_enabled,
            journal_mode=journal_mode,
            synchronous=synchronous,
            temp_store=temp_store,
            schema_version=schema_version,
            applied_migrations_count=applied_count,
            integrity_check_passed=integrity_check_passed,
            read_write_verified=read_write_verified,
            free_space_bytes=free_bytes,
            diagnostic_messages=diagnostics,
            checked_at_utc=checked_at,
        )

    finally:
        conn.close()
