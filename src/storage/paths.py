"""SAGE Local SQLite Storage Foundation - Path Resolution & Safety Boundaries.

Document Reference: docs/specifications/AEGIS_SQLITE_STORAGE_SUBSYSTEM.md
Constraints:
- Default runtime database MUST reside outside OneDrive (%USERPROFILE%\\.aegis\\data\\aegis.db).
- Custom database paths inside OneDrive, the canonical repo, or C:\\AEGIS are strictly forbidden.
- Fails closed with ForbiddenDatabaseLocationError rather than silently relocating files.
- Deterministic, Windows-safe, zero hardcoded usernames.
"""

import os
from pathlib import Path
from typing import Any, Optional

from src.storage.errors import ForbiddenDatabaseLocationError

# Default canonical roots for boundary enforcement
CANONICAL_RECOVERY_ROOT = Path("C:/AEGIS").resolve()


def get_default_sage_home() -> Path:
    """Return root SAGE local home directory (%USERPROFILE%\\.aegis as legacy/current storage)."""
    override = os.environ.get("SAGE_HOME") or os.environ.get("AEGIS_HOME")
    if override:
        return Path(override).resolve()
    return (Path.home() / ".aegis").resolve()


# Backward compatibility alias
get_default_aegis_home = get_default_sage_home


def get_default_data_dir() -> Path:
    """Return local data directory for persistent databases."""
    return get_default_sage_home() / "data"


def get_default_db_path() -> Path:
    """Return canonical default SQLite database path (%USERPROFILE%\\.aegis\\data\\aegis.db)."""
    return get_default_data_dir() / "aegis.db"


def get_default_runtime_dir() -> Path:
    """Return local runtime directory for transient execution state."""
    return get_default_aegis_home() / "runtime"


def get_default_logs_dir() -> Path:
    """Return local directory for operational and diagnostic logs."""
    return get_default_aegis_home() / "logs"


def get_default_cache_dir() -> Path:
    """Return local directory for ephemeral derived caches."""
    return get_default_aegis_home() / "cache"


def get_default_backups_dir() -> Path:
    """Return local directory for pre-migration and recovery backup snapshots."""
    return get_default_aegis_home() / "backups"


def _get_canonical_repo_root() -> Path:
    """Locate current canonical Git repository root safely."""
    # Anchored relative to this file: src/storage/paths.py -> root is 2 levels up
    return Path(__file__).resolve().parent.parent.parent


def is_path_inside_onedrive(path: Path | str) -> bool:
    """Check whether the given path resides inside any Microsoft OneDrive directory."""
    resolved = Path(path).resolve()
    # Check path parts
    for part in resolved.parts:
        if "onedrive" in part.lower():
            return True

    # Check environment variables pointing to OneDrive roots
    for env_var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        od_root = os.environ.get(env_var)
        if od_root:
            od_path = Path(od_root).resolve()
            try:
                resolved.relative_to(od_path)
                return True
            except ValueError:
                pass
    return False


def is_path_inside_repo(path: Path | str, repo_root: Optional[Path] = None) -> bool:
    """Check whether the given path resides inside the canonical Git source repository."""
    resolved = Path(path).resolve()
    root = (repo_root or _get_canonical_repo_root()).resolve()
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def is_path_inside_recovery_repo(path: Path | str, recovery_root: Optional[Path] = None) -> bool:
    """Check whether the given path resides inside the C:\\AEGIS recovery repository."""
    resolved = Path(path).resolve()
    root = (recovery_root or CANONICAL_RECOVERY_ROOT).resolve()
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def validate_database_path(path: Path | str) -> Path:
    """Validate that target database path does not violate forbidden storage boundaries.

    Raises:
        ForbiddenDatabaseLocationError: If path resolves inside OneDrive, Git repo,
            or recovery copy.
    """
    resolved = Path(path).resolve()

    if is_path_inside_recovery_repo(resolved):
        raise ForbiddenDatabaseLocationError(
            f"Active SQLite database cannot reside inside C:\\AEGIS: '{resolved}'. "
            "C:\\AEGIS is a recovery copy and must not host active execution state."
        )

    if is_path_inside_repo(resolved):
        raise ForbiddenDatabaseLocationError(
            f"Active SQLite database cannot reside inside Git source repository: '{resolved}'. "
            "Runtime databases must remain strictly decoupled from source control trees."
        )

    if is_path_inside_onedrive(resolved):
        raise ForbiddenDatabaseLocationError(
            f"Active SQLite database cannot reside inside OneDrive: '{resolved}'. "
            "OneDrive sync locks WAL files (*.db-wal, *.db-shm), causes sync races, "
            "and degrades I/O. Use '%USERPROFILE%\\.aegis\\data\\aegis.db' or local path."
        )

    return resolved


def resolve_database_path(
    config: Optional[Any] = None,
    custom_path: Optional[str | Path] = None,
    ensure_dir: bool = True,
) -> Path:
    """Resolve, validate, and optionally prepare parent directory for SQLite database.

    Precedence:
    1. Explicit custom_path argument (if provided)
    2. config.storage.database_path (if configured)
    3. Canonical default: %USERPROFILE%\\.aegis\\data\\aegis.db

    Raises:
        ForbiddenDatabaseLocationError: If resolved path violates boundary safety rules.
    """
    if custom_path is not None:
        target = validate_database_path(custom_path)
    elif config is not None and getattr(getattr(config, "storage", None), "database_path", None):
        target = validate_database_path(config.storage.database_path)
    else:
        target = validate_database_path(get_default_db_path())

    if ensure_dir:
        target.parent.mkdir(parents=True, exist_ok=True)

    return target
