"""SAGE Database Master Key Rotation & Disaster Recovery Subsystem.

Canonical Reference:
    AEGIS Prompt 040: Database Key Rotation & Recovery
    docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md

Invariants:
- Never replaces the master key without coordinating the database.
- Uses atomic staging and native SQLCipher sqlcipher_export for zero-data-loss rekeying.
- Maintains a durable rotation journal to ensure deterministic recovery across crashes.
- Cross-process exclusive locking prevents concurrent rotations or startup races.
- Verifies database integrity and schema/data parity before committing any changes.
- Archives historical keys so historical backups remain decryptable by version.
- Re-keys backups in the backup directory to preserve current-key restore compatibility.
- Zero plaintext key or database fallback; fails closed on any verification anomaly.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlcipher3

from src.security.crypto import KEY_LENGTH_BYTES, AES256GCMCipher
from src.security.database_key import DatabaseKeyError, DatabaseKeyManager
from src.storage.paths import get_default_backups_dir, validate_database_path

logger = logging.getLogger(__name__)


class RotationState(str, Enum):
    """Lifecycle states of the database key rotation transaction."""

    IDLE = "IDLE"
    PREPARING = "PREPARING"
    SNAPSHOT_CREATED = "SNAPSHOT_CREATED"
    STAGING_EXPORTED = "STAGING_EXPORTED"
    STAGING_VERIFIED = "STAGING_VERIFIED"
    KEY_COMMITTED = "KEY_COMMITTED"
    DATABASE_COMMITTED = "DATABASE_COMMITTED"
    BACKUPS_SYNCHRONIZED = "BACKUPS_SYNCHRONIZED"
    COMPLETED = "COMPLETED"


class RotationError(RuntimeError):
    """Base exception for database key rotation failures."""


class RotationLockError(RotationError):
    """Raised when exclusive database rotation lock cannot be acquired."""


class RotationDrainTimeoutError(RotationError):
    """Raised when active database connections cannot be drained within timeout."""


class ExclusiveDatabaseLock:
    """Manages cross-process exclusive locking for database maintenance operations."""

    def __init__(self, db_path: Path, timeout_sec: float = 30.0) -> None:
        self._db_path = db_path
        self._lock_path = db_path.with_name(f"{db_path.name}.rotation.lock")
        self._timeout_sec = timeout_sec
        self._fd: Optional[int] = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def __enter__(self) -> ExclusiveDatabaseLock:
        start_time = time.time()
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                self._fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
                lock_payload = f"{os.getpid()}:{datetime.now(timezone.utc).isoformat()}"
                os.write(self._fd, lock_payload.encode("utf-8"))
                return self
            except FileExistsError as err:
                # Check for stale lock
                if self._is_lock_stale():
                    self._break_stale_lock()
                    continue

                if time.time() - start_time >= self._timeout_sec:
                    raise RotationLockError(
                        f"Timed out waiting for exclusive rotation lock on '{self._db_path}'. "
                        f"Lock held at '{self._lock_path}'."
                    ) from err
                time.sleep(0.1)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None

        if self._lock_path.is_file():
            try:
                self._lock_path.unlink()
            except Exception:
                pass

    def _is_lock_stale(self) -> bool:
        """Check if existing lock file belongs to a terminated process or has expired."""
        try:
            stat = self._lock_path.stat()
            # If lock is older than 5 minutes, consider it stale
            if time.time() - stat.st_mtime > 300:
                return True

            content = self._lock_path.read_text(encoding="utf-8").strip()
            if ":" in content:
                pid_str = content.split(":", 1)[0]
                pid = int(pid_str)
                # Check if process is still running
                return not self._is_pid_alive(pid)
        except Exception:
            pass
        return False

    def _break_stale_lock(self) -> None:
        """Break a verified stale lock file."""
        try:
            logger.warning("Breaking stale rotation lock at '%s'", self._lock_path)
            self._lock_path.unlink()
        except Exception:
            pass

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if process ID is alive on current platform without sending signals."""
        if pid <= 0:
            return False
        if sys.platform == "win32":
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    exit_code = ctypes.c_ulong()
                    if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        # STILL_ACTIVE = 259
                        is_active = (exit_code.value == 259)
                    else:
                        is_active = True
                    kernel32.CloseHandle(handle)
                    return is_active
                else:
                    # ERROR_ACCESS_DENIED = 5 (process exists but owned by other user)
                    return kernel32.GetLastError() == 5
            except Exception:
                return False

        try:
            # On POSIX, os.kill(pid, 0) checks existence without signaling
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
        except PermissionError:
            # Running under another user, definitely alive
            return True


class RotationJournal:
    """Manages transactional state persistence for key rotation recovery."""

    def __init__(self, db_path: Path) -> None:
        self._journal_path = db_path.with_name(f"{db_path.name}.rotation_journal.json")

    @property
    def path(self) -> Path:
        return self._journal_path

    def exists(self) -> bool:
        return self._journal_path.is_file()

    def read(self) -> Optional[Dict[str, Any]]:
        if not self._journal_path.is_file():
            return None
        try:
            loaded = json.loads(self._journal_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
            return None
        except Exception as exc:
            logger.warning("Failed to read rotation journal '%s': %s", self._journal_path, exc)
            return None

    def record_state(self, state: RotationState, **kwargs: Any) -> None:
        """Atomically record rotation state to journal."""
        existing = self.read() or {}
        existing["state"] = state.value
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        existing.update(kwargs)

        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = self._journal_path.parent
        with tempfile.NamedTemporaryFile(
            dir=temp_dir, mode="w", delete=False, encoding="utf-8"
        ) as tf:
            json.dump(existing, tf, indent=2)
            temp_name = tf.name

        for attempt in range(3):
            try:
                os.replace(temp_name, self._journal_path)
                break
            except PermissionError as pe:
                if attempt == 2:
                    if os.path.exists(temp_name):
                        os.remove(temp_name)
                    raise RotationError(f"Failed to record rotation journal state: {pe}") from pe
                time.sleep(0.02 * (2 ** attempt))
            except Exception as exc:
                if os.path.exists(temp_name):
                    os.remove(temp_name)
                raise RotationError(f"Failed to record rotation journal state: {exc}") from exc

    def delete(self) -> None:
        """Delete rotation journal upon clean transaction completion."""
        if self._journal_path.is_file():
            try:
                self._journal_path.unlink()
            except Exception:
                pass


def rotate_database_key(
    db_path: Path | str,
    key_manager: Optional[DatabaseKeyManager] = None,
    new_key: Optional[bytes] = None,
    backup_dir: Optional[Path | str] = None,
    timeout_sec: float = 30.0,
    drain_timeout_sec: float = 15.0,
) -> Dict[str, Any]:
    """Execute a coordinated, atomic, and recoverable database master key rotation.

    Transaction Workflow:
    1. Validate database path and acquire cross-process exclusive lock.
    2. Drain all active and idle database connections across threads/processes.
    3. Check for and recover any previous interrupted rotation.
    4. Flush active transactions and checkpoint WAL to ensure consistency.
    5. Create and verify a pre-rotation recovery snapshot under the current key.
    6. Export the database to a staged database under the fresh key via sqlcipher_export.
    7. Verify staged database: integrity check, schema/row parity, fail-closed old key check.
    8. Recheck source database state to ensure zero writes slipped in during export.
    9. Atomically archive current key to version history and commit fresh key via DPAPI.
    10. Atomically replace active database with verified staged database (with bounded Windows retries).
    11. Re-key existing backups in backup_dir to maintain single-key restore compatibility.
    12. Finalize journal and release lock and connection drain.

    Args:
        db_path: Path to the target SQLCipher database.
        key_manager: Key manager instance. If None, uses default.
        new_key: Optional explicit 32-byte key (for tests). If None, generates random CSPRNG.
        backup_dir: Optional backup directory for backup sync. If None, uses default backups dir.
        timeout_sec: Timeout waiting for exclusive lock.
        drain_timeout_sec: Timeout waiting for active connections to drain.

    Returns:
        Dictionary containing rotation metadata and verification metrics.

    Raises:
        RotationError: If any verification fails or rotation cannot complete.
        RotationLockError: If exclusive database lock cannot be acquired.
        RotationDrainTimeoutError: If active connections cannot be drained within timeout.
    """
    path = Path(db_path).resolve()
    validated_path = validate_database_path(path)
    if not validated_path.is_file():
        raise RotationError(f"Target database file does not exist: '{validated_path}'")

    mgr = key_manager or DatabaseKeyManager()
    journal = RotationJournal(validated_path)

    # 1. Acquire exclusive lock
    with ExclusiveDatabaseLock(validated_path, timeout_sec=timeout_sec):
        from src.storage.database import (
            ConnectionCoordinator,
            DatabaseAccessLock,
            can_acquire_exclusive_file_access,
        )

        access_lock = DatabaseAccessLock(validated_path)

        try:
            # 2. Drain connections across threads and processes
            ConnectionCoordinator.drain(validated_path, timeout_sec=drain_timeout_sec)

            # 2.5 Acquire OS-level cross-process exclusive access lock across the critical section
            if not access_lock.acquire_exclusive(timeout_sec=timeout_sec):
                raise RotationLockError(
                    f"Timed out waiting for exclusive database access lock on '{validated_path}'."
                )
            # 3. Check for and recover any interrupted rotation
            if journal.exists():
                recovery_info = recover_interrupted_rotation(
                    validated_path, key_manager=mgr, release_drain=False
                )
                logger.info("Executed recovery before rotation: %s", recovery_info)

            # 4. Active key resolution & WAL checkpoint
            current_key = mgr.get_key()
            if current_key is None:
                raise DatabaseKeyError(
                    f"Cannot rotate key: master key file missing for database '{validated_path}'."
                )

            hex_old_key = current_key.hex()

            # Verify current key authenticates against active database
            chk_conn = None
            try:
                chk_conn = sqlcipher3.connect(str(validated_path))
                chk_conn.execute(f"PRAGMA key = \"x'{hex_old_key}'\";")
                chk_conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
                wal_chk = chk_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
                if wal_chk is not None and wal_chk[0] == 1:
                    raise RotationError(
                        f"WAL checkpoint failed to complete (busy=1): {wal_chk}"
                    )
            except Exception as exc:
                raise RotationError(
                    f"Failed to authenticate active database or checkpoint WAL: {exc}"
                ) from exc
            finally:
                if chk_conn is not None:
                    try:
                        chk_conn.close()
                    except Exception as close_exc:
                        logger.warning("Error closing chk_conn on %s: %s", validated_path, close_exc)

            # Collect baseline schema and row counts for parity verification
            baseline_counts = _collect_database_parity(validated_path, current_key)

            journal_id = uuid4().hex[:8]
            journal.record_state(
                RotationState.PREPARING,
                journal_id=journal_id,
                db_path=str(validated_path),
                pid=os.getpid(),
                old_version=mgr.get_current_version(),
            )

            # 5. Pre-rotation recovery snapshot
            snapshot_file = validated_path.with_name(
                f"{validated_path.stem}.pre_rotation_{journal_id}.db"
            )
            snapshot_staging = snapshot_file.with_name(f"{snapshot_file.name}.staging")
            if snapshot_staging.exists():
                try:
                    snapshot_staging.unlink()
                except Exception:
                    pass

            snap_conn = None
            v_snap = None
            try:
                snap_conn = sqlcipher3.connect(str(validated_path))
                snap_conn.execute(f"PRAGMA key = \"x'{hex_old_key}'\";")
                snap_conn.execute(
                    f"ATTACH DATABASE '{snapshot_staging}' AS snap KEY \"x'{hex_old_key}'\";"
                )
                snap_conn.execute("SELECT sqlcipher_export('snap');")
                snap_conn.execute("DETACH DATABASE snap;")
                snap_conn.close()
                snap_conn = None

                # Verify snapshot
                v_snap = sqlcipher3.connect(str(snapshot_staging))
                v_snap.execute(f"PRAGMA key = \"x'{hex_old_key}'\";")
                chk_res = v_snap.execute("PRAGMA integrity_check;").fetchone()
                v_snap.close()
                v_snap = None
                if chk_res is None or str(chk_res[0]).lower() != "ok":
                    raise RotationError(f"Snapshot integrity check failed: {chk_res}")

                os.replace(snapshot_staging, snapshot_file)
                journal.record_state(
                    RotationState.SNAPSHOT_CREATED,
                    snapshot_path=str(snapshot_file),
                )
                logger.info("Created pre-rotation recovery snapshot at '%s'", snapshot_file)
            except Exception as exc:
                if snapshot_staging.exists():
                    try:
                        snapshot_staging.unlink()
                    except Exception:
                        pass
                journal.delete()
                raise RotationError(f"Failed to create pre-rotation recovery snapshot: {exc}") from exc
            finally:
                if snap_conn is not None:
                    try:
                        snap_conn.close()
                    except Exception:
                        pass
                if v_snap is not None:
                    try:
                        v_snap.close()
                    except Exception:
                        pass

            # 6. Staging re-keyed database
            fresh_key = new_key or AES256GCMCipher.generate_random_key()
            if len(fresh_key) != KEY_LENGTH_BYTES:
                journal.delete()
                raise RotationError(f"New key must be exactly {KEY_LENGTH_BYTES} bytes.")

            if fresh_key == current_key:
                journal.delete()
                raise RotationError("New rotation key cannot be identical to current key.")

            hex_new_key = fresh_key.hex()
            staging_db_file = validated_path.with_name(
                f"{validated_path.name}.rekey_{journal_id}.staging"
            )
            if staging_db_file.exists():
                try:
                    staging_db_file.unlink()
                except Exception:
                    pass

            export_conn = None
            try:
                export_conn = sqlcipher3.connect(str(validated_path))
                export_conn.execute(f"PRAGMA key = \"x'{hex_old_key}'\";")
                export_conn.execute(
                    f"ATTACH DATABASE '{staging_db_file}' AS staged KEY \"x'{hex_new_key}'\";"
                )
                export_conn.execute("SELECT sqlcipher_export('staged');")
                export_conn.execute("DETACH DATABASE staged;")
                export_conn.close()
                export_conn = None

                journal.record_state(
                    RotationState.STAGING_EXPORTED,
                    staging_db_path=str(staging_db_file),
                )
            except Exception as exc:
                if staging_db_file.exists():
                    try:
                        staging_db_file.unlink()
                    except Exception:
                        pass
                journal.delete()
                raise RotationError(f"Failed to export re-keyed database to staging: {exc}") from exc
            finally:
                if export_conn is not None:
                    try:
                        export_conn.close()
                    except Exception:
                        pass

            # 7. Verify staged database
            try:
                # Positive verification with new key
                staged_conn = None
                try:
                    staged_conn = sqlcipher3.connect(str(staging_db_file))
                    staged_conn.execute(f"PRAGMA key = \"x'{hex_new_key}'\";")
                    staged_chk = staged_conn.execute("PRAGMA integrity_check;").fetchone()
                    if staged_chk is None or str(staged_chk[0]).lower() != "ok":
                        raise RotationError(f"Staged database integrity check failed: {staged_chk}")
                finally:
                    if staged_conn is not None:
                        try:
                            staged_conn.close()
                        except Exception as close_exc:
                            logger.warning("Error closing staged_conn: %s", close_exc)

                # Parity verification
                staged_counts = _collect_database_parity(staging_db_file, fresh_key)
                if staged_counts != baseline_counts:
                    raise RotationError(
                        f"Data parity mismatch between original and staged rekeyed database: "
                        f"original={baseline_counts}, staged={staged_counts}"
                    )

                # Fail-closed verification with old key
                # (guarantees handle closure & differentiates errors)
                _verify_key_rejected(staging_db_file, current_key)

                journal.record_state(RotationState.STAGING_VERIFIED)
            except Exception as exc:
                if staging_db_file.exists():
                    try:
                        staging_db_file.unlink()
                    except Exception:
                        pass
                journal.delete()
                raise RotationError(f"Verification of staged re-keyed database failed: {exc}") from exc

            # 7.5 Pre-swap verification: ensure no writes slipped in on active database
            # SEC-044-02 & SEC-044-06: compare SHA-256 schema and row-content fingerprints
            # to detect INSERT, DELETE, UPDATE, DDL, and trigger mutations.
            wal_file = validated_path.with_name(f"{validated_path.name}-wal")

            # Checkpoint WAL and ensure no uncheckpointed frames remain
            wal_conn = None
            try:
                wal_conn = sqlcipher3.connect(str(validated_path))
                wal_conn.execute(f"PRAGMA key = \"x'{hex_old_key}'\";")
                wal_chk = wal_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
                if wal_chk is not None and wal_chk[0] == 1:
                    raise RotationError(f"WAL checkpoint busy before swap (busy=1): {wal_chk}")
            finally:
                if wal_conn is not None:
                    try:
                        wal_conn.close()
                    except Exception:
                        pass

            # Abort if WAL has uncheckpointed data
            if wal_file.is_file() and wal_file.stat().st_size > 0:
                if staging_db_file.exists():
                    try:
                        staging_db_file.unlink()
                    except Exception:
                        pass
                journal.delete()
                raise RotationError(
                    f"WAL file has uncheckpointed data ({wal_file.stat().st_size} bytes). Aborting swap."
                )

            # Parity verification
            current_counts = _collect_database_parity(validated_path, current_key)
            if current_counts != baseline_counts:
                if staging_db_file.exists():
                    try:
                        staging_db_file.unlink()
                    except Exception:
                        pass
                journal.delete()
                raise RotationError(
                    f"Active database modified during rotation export: "
                    f"baseline={baseline_counts}, current={current_counts}. Rotation aborted."
                )

            # Validate that no external process holds an open handle before key commitment
            exclusive_acquired = False
            exclusive_start = time.time()
            exclusive_timeout = min(5.0, drain_timeout_sec)
            while time.time() - exclusive_start < exclusive_timeout:
                if can_acquire_exclusive_file_access(validated_path):
                    if not wal_file.is_file() or can_acquire_exclusive_file_access(wal_file):
                        exclusive_acquired = True
                        break
                time.sleep(0.05)

            if not exclusive_acquired:
                if staging_db_file.exists():
                    try:
                        staging_db_file.unlink()
                    except Exception:
                        pass
                journal.delete()
                raise RotationError(
                    f"Cannot acquire exclusive file access to '{validated_path}' before database swap."
                )

            pre_swap_mtime_ns = validated_path.stat().st_mtime_ns
            pre_swap_size = validated_path.stat().st_size

            # 8. Key state publication
            current_version = mgr.get_current_version()
            new_version = current_version + 1
            archived_key_path = mgr.archive_current_key(current_version)
            mgr.commit_new_key(fresh_key, new_version=new_version)

            journal.record_state(
                RotationState.KEY_COMMITTED,
                archived_key_path=str(archived_key_path),
                new_version=new_version,
            )

            # 9. Database publication
            # Pre-replace invariant check immediately prior to os.replace
            post_conn = None
            try:
                replace_attempts = 5
                for attempt in range(replace_attempts):
                    # Re-verify invariant before every replacement attempt
                    current_mtime = validated_path.stat().st_mtime_ns
                    current_size = validated_path.stat().st_size
                    current_wal = wal_file.stat().st_size if wal_file.is_file() else 0
                    if (
                        current_mtime != pre_swap_mtime_ns
                        or current_size != pre_swap_size
                        or current_wal > 0
                    ):
                        mgr.rollback_key(current_version)
                        if staging_db_file.exists():
                            try:
                                staging_db_file.unlink()
                            except Exception:
                                pass
                        journal.delete()
                        raise RotationError(
                            f"Active database was modified immediately prior to swap. "
                            f"Key rolled back to version {current_version}; rotation aborted."
                        )

                    try:
                        os.replace(staging_db_file, validated_path)
                        break
                    except PermissionError:
                        if attempt == replace_attempts - 1:
                            mgr.rollback_key(current_version)
                            if staging_db_file.exists():
                                try:
                                    staging_db_file.unlink()
                                except Exception:
                                    pass
                            journal.delete()
                            raise RotationError(
                                f"Cannot replace database file '{validated_path}': file is open by another process. "
                                f"Key rolled back to version {current_version}; rotation aborted fail-closed."
                            )
                        time.sleep(0.05 * (2 ** attempt))

                # Remove leftover 0-byte WAL and SHM files from the source database
                for ext in ("-wal", "-shm"):
                    lingering = validated_path.with_name(f"{validated_path.name}{ext}")
                    if lingering.exists():
                        try:
                            if ext == "-wal" and lingering.stat().st_size > 0:
                                logger.warning("Retaining non-empty WAL file '%s'", lingering)
                                continue
                            lingering.unlink()
                        except Exception:
                            pass

                # Post-replacement verification with a fresh connection to the new file
                post_conn = sqlcipher3.connect(str(validated_path))
                post_conn.execute(f"PRAGMA key = \"x'{hex_new_key}'\";")
                post_chk = post_conn.execute("PRAGMA integrity_check;").fetchone()
                if post_chk is None or str(post_chk[0]).lower() != "ok":
                    raise RotationError(f"Post-publication integrity check failed: {post_chk}")
                post_conn.close()
                post_conn = None

                journal.record_state(RotationState.DATABASE_COMMITTED)
                logger.info("Published re-keyed database at '%s'", validated_path)

            except RotationError:
                raise
            except Exception as exc:
                # Fatal error during publication: trigger immediate recovery
                logger.critical("Fatal error publishing re-keyed database: %s", exc)
                recovery_res = recover_interrupted_rotation(
                    validated_path, key_manager=mgr, release_drain=False
                )
                raise RotationError(
                    f"Failed to publish re-keyed database (recovery invoked: {recovery_res}): {exc}"
                ) from exc
            finally:
                if post_conn is not None:
                    try:
                        post_conn.close()
                    except Exception as close_exc:
                        logger.warning("Error closing post_conn: %s", close_exc)

            # 10. Backup synchronization
            synchronized_backups: List[str] = []
            b_dir = Path(backup_dir) if backup_dir else get_default_backups_dir()
            if b_dir.is_dir():
                for b_file in b_dir.glob("*.db"):
                    if b_file.resolve() == validated_path or b_file.resolve() == snapshot_file:
                        continue
                    rekeyed = _rekey_backup_file(b_file, current_key, fresh_key)
                    if rekeyed:
                        synchronized_backups.append(b_file.name)

            journal.record_state(
                RotationState.BACKUPS_SYNCHRONIZED,
                synchronized_backups=synchronized_backups,
            )

            # 11. Clean up journal and complete
            journal.delete()
            logger.info(
                "Successfully completed database key rotation for '%s' (v%d -> v%d)",
                validated_path,
                current_version,
                new_version,
            )

            return {
                "status": "SUCCESS",
                "db_path": str(validated_path),
                "old_version": current_version,
                "new_version": new_version,
                "snapshot_path": str(snapshot_file),
                "synchronized_backups": synchronized_backups,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "old_key": current_key,
                "new_key": fresh_key,
            }
        finally:
            access_lock.release()
            ConnectionCoordinator.release_drain(validated_path)


def recover_interrupted_rotation(
    db_path: Path | str,
    key_manager: Optional[DatabaseKeyManager] = None,
    release_drain: bool = True,
) -> Dict[str, Any]:
    """Inspect and deterministically recover from an interrupted database key rotation.

    Recovery Matrix:
    - Active key decrypts staging DB -> Roll-Forward: replace active database with staging DB.
    - Active key decrypts active DB -> Roll-Back: Discard temporary staging artifacts.
    - Active key decrypts neither, archived key decrypts active DB -> Roll-Back Key:
      Restore key file from archived key and discard staging artifacts.

    Args:
        db_path: Target database path.
        key_manager: Key manager instance (optional).
        release_drain: Whether to clean up advisory .drain file on recovery completion.

    Returns:
        Recovery summary dictionary.
    """
    path = Path(db_path).resolve()
    from src.storage.database import ConnectionCoordinator

    try:
        journal = RotationJournal(path)
        if not journal.exists():
            return {
                "status": "NOOP",
                "message": "No rotation journal found; database is consistent.",
            }

        j_data = journal.read() or {}
        state_str = j_data.get("state", RotationState.IDLE.value)
        staging_str = j_data.get("staging_db_path")
        archived_key_str = j_data.get("archived_key_path")
        old_ver = j_data.get("old_version", 1)

        staging_path = Path(staging_str) if staging_str else None
        archived_key_path = Path(archived_key_str) if archived_key_str else None

        mgr = key_manager or DatabaseKeyManager()
        active_key = mgr.get_key()

        logger.warning(
            "Interrupted key rotation detected for '%s' at state '%s'. Initiating recovery...",
            path,
            state_str,
        )

        # Deterministic recovery based on cryptographic authentication
        if active_key is not None:
            # Case 1: Active key authenticates staging DB -> Roll-forward
            if staging_path and staging_path.is_file():
                conn1 = None
                try:
                    conn1 = sqlcipher3.connect(str(staging_path))
                    conn1.execute(f"PRAGMA key = \"x'{active_key.hex()}'\";")
                    chk1 = conn1.execute("PRAGMA integrity_check;").fetchone()
                    conn1.close()
                    conn1 = None
                    if chk1 and str(chk1[0]).lower() == "ok":
                        os.replace(staging_path, path)
                        journal.delete()
                        logger.info(
                            "Recovered by roll-forward: staging database published to '%s'", path
                        )
                        return {
                            "status": "ROLLED_FORWARD",
                            "message": "Staged database was verified and successfully published.",
                        }
                except Exception as exc:
                    logger.warning("Staging database failed roll-forward authentication: %s", exc)
                finally:
                    if conn1 is not None:
                        try:
                            conn1.close()
                        except Exception:
                            pass

            # Case 2: Active key authenticates active DB -> Roll-back
            if path.is_file():
                conn2 = None
                try:
                    conn2 = sqlcipher3.connect(str(path))
                    conn2.execute(f"PRAGMA key = \"x'{active_key.hex()}'\";")
                    chk2 = conn2.execute("PRAGMA integrity_check;").fetchone()
                    conn2.close()
                    conn2 = None
                    if chk2 and str(chk2[0]).lower() == "ok":
                        if staging_path and staging_path.is_file():
                            try:
                                staging_path.unlink()
                            except Exception:
                                pass
                        journal.delete()
                        logger.info(
                            "Recovered by roll-back: original database verified under active key."
                        )
                        return {
                            "status": "ROLLED_BACK",
                            "message": "Original database verified; discarded staging artifacts.",
                        }
                except Exception:
                    pass
                finally:
                    if conn2 is not None:
                        try:
                            conn2.close()
                        except Exception:
                            pass

        # Case 3: Active key does not open active DB, but archived key does -> Roll-back key file
        if archived_key_path and archived_key_path.is_file() and path.is_file():
            conn3 = None
            try:
                archived_key = mgr.get_key(version=old_ver)
                if archived_key is not None:
                    conn3 = sqlcipher3.connect(str(path))
                    conn3.execute(f"PRAGMA key = \"x'{archived_key.hex()}'\";")
                    chk3 = conn3.execute("PRAGMA integrity_check;").fetchone()
                    conn3.close()
                    conn3 = None
                    if chk3 and str(chk3[0]).lower() == "ok":
                        mgr.rollback_key(old_ver)
                        if staging_path and staging_path.is_file():
                            try:
                                staging_path.unlink()
                            except Exception:
                                pass
                        journal.delete()
                        logger.info("Recovered by rolling back key file to version %d", old_ver)
                        return {
                            "status": "ROLLED_BACK",
                            "message": f"Rolled back key file to version {old_ver}.",
                        }
            except Exception as exc:
                logger.critical("Failed to rollback key file during recovery: %s", exc)
            finally:
                if conn3 is not None:
                    try:
                        conn3.close()
                    except Exception:
                        pass

        # Clean up journal if corrupted
        journal.delete()
        return {
            "status": "FAILED",
            "message": "Automatic recovery could not determine authoritative state.",
        }
    finally:
        if release_drain:
            ConnectionCoordinator.release_drain(path)


def _verify_key_rejected(staged_path: Path, old_key: bytes) -> None:
    """Verify that staged database fails closed when accessed with old_key.

    Ensures that a genuine authentication failure occurs and distinguishes
    it from filesystem or environmental errors. Guarantees connection closure.
    """
    if not staged_path.is_file():
        raise RotationError(
            f"Staged database does not exist for old-key verification: '{staged_path}'"
        )

    fail_conn: Optional[sqlcipher3.Connection] = None
    auth_failed = False
    try:
        fail_conn = sqlcipher3.connect(str(staged_path))
        fail_conn.execute(f"PRAGMA key = \"x'{old_key.hex()}'\";")
        # Attempt to read schema metadata: must fail if old_key is invalid
        fail_conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError) as exc:
        err_str = str(exc).lower()
        if "file is not a database" in err_str or "hmac" in err_str or "decrypt" in err_str:
            auth_failed = True
            logger.debug("Old key rejected as expected with SQLCipher error: %s", exc)
        else:
            raise RotationError(
                f"Unexpected database error during old-key rejection check "
                f"on '{staged_path}': {exc}"
            ) from exc
    except (OSError, PermissionError) as fs_err:
        raise RotationError(
            f"Filesystem/permission error during old-key rejection check "
            f"on '{staged_path}': {fs_err}"
        ) from fs_err
    except Exception as unexpected:
        raise RotationError(
            f"Unexpected non-authentication failure during old-key rejection check: {unexpected}"
        ) from unexpected
    finally:
        if fail_conn is not None:
            try:
                fail_conn.close()
            except Exception as close_exc:
                logger.warning(
                    "Error closing connection during old-key verification: %s", close_exc
                )

    if not auth_failed:
        raise RotationError(
            f"Security assertion failed: staged database '{staged_path}' "
            f"unexpectedly opened with old key!"
        )


def _collect_database_parity(db_path: Path, key: bytes) -> Dict[str, str]:
    """Collect a deterministic SHA-256 fingerprint for schema and all row content.

    The parity dictionary contains:
    - ``__schema_hash__``: SHA-256 of every sqlite_master entry (type, name, tbl_name, sql)
      ordered deterministically.  This detects DDL changes: CREATE/DROP TABLE, CREATE/DROP
      INDEX, CREATE/DROP TRIGGER, etc.
    - one entry per user table mapping ``table_name`` to ``"<row_count>:<SHA-256-of-rows>"``.
      Rows are serialised with NULL-safe string encoding and ordered by all columns so that
      UPDATE, paired DELETE+INSERT, and trigger mutations are detected in addition to plain
      INSERT / DELETE operations.

    Using SHA-256 rather than XOR-CRC32 eliminates the symmetry cancellation flaw in the
    previous implementation (two rows with the same CRC XOR to 0, indistinguishable from an
    empty table).  (SEC-044-06 strength upgrade.)
    """
    import hashlib

    parity: Dict[str, str] = {}
    conn = sqlcipher3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\";")

        # 1. Schema fingerprint — detects DDL mutations
        schema_rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name;"
        ).fetchall()
        sh = hashlib.sha256()
        for r in schema_rows:
            sh.update(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}\n".encode("utf-8"))
        parity["__schema_hash__"] = sh.hexdigest()

        # 2. Per-table row content fingerprint
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name;"
            ).fetchall()
        ]
        for tbl in sorted(tables):
            c_row = conn.execute(f'SELECT count(*) FROM "{tbl}";').fetchone()
            row_count = c_row[0] if c_row else 0
            rh = hashlib.sha256()
            # Deterministic column order for row sorting
            cols = [
                f'"{col[1]}"'
                for col in conn.execute(f'PRAGMA table_info("{tbl}");').fetchall()
            ]
            order_clause = f"ORDER BY {', '.join(cols)}" if cols else ""
            cursor = conn.execute(f'SELECT * FROM "{tbl}" {order_clause};')
            for data_row in cursor:
                # Use length prefixing to eliminate delimiter collision
                parts = []
                for v in data_row:
                    if v is None:
                        parts.append("-1:N")
                    elif isinstance(v, bytes):
                        parts.append(f"{len(v)}:B{v.hex()}")
                    else:
                        s = str(v)
                        parts.append(f"{len(s)}:S{s}")
                rh.update(",".join(parts).encode("utf-8") + b"\n")
            parity[tbl] = f"{row_count}:{rh.hexdigest()}"
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return parity


# Backward-compatible alias; tests and internal callers may reference either name.
_collect_table_counts = _collect_database_parity


def _rekey_backup_file(backup_path: Path, old_key: bytes, new_key: bytes) -> bool:
    """Safely re-key an encrypted backup file from old_key to new_key."""
    staging_file = backup_path.with_name(f"{backup_path.name}.rekey_{uuid4().hex[:8]}.staging")
    if staging_file.exists():
        try:
            staging_file.unlink()
        except Exception:
            pass

    conn = None
    v_conn = None
    try:
        conn = sqlcipher3.connect(str(backup_path))
        conn.execute(f"PRAGMA key = \"x'{old_key.hex()}'\";")
        # Test if backup opens with old_key
        conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()

        conn.execute(f"ATTACH DATABASE '{staging_file}' AS rekeyed KEY \"x'{new_key.hex()}'\";")
        conn.execute("SELECT sqlcipher_export('rekeyed');")
        conn.execute("DETACH DATABASE rekeyed;")
        conn.close()
        conn = None

        # Verify staging backup
        v_conn = sqlcipher3.connect(str(staging_file))
        v_conn.execute(f"PRAGMA key = \"x'{new_key.hex()}'\";")
        chk = v_conn.execute("PRAGMA integrity_check;").fetchone()
        v_conn.close()
        v_conn = None

        if chk and str(chk[0]).lower() == "ok":
            os.replace(staging_file, backup_path)
            logger.info("Successfully re-keyed backup file '%s' to new key", backup_path)
            return True
        else:
            if staging_file.exists():
                staging_file.unlink()
            return False
    except Exception:
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if v_conn is not None:
            try:
                v_conn.close()
            except Exception:
                pass

