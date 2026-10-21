"""SAGE Database Master Key Management Subsystem.

Canonical Reference:
    AEGIS Prompt 037: Whole-Database Encryption
    docs/AEGIS_DATABASE_ENCRYPTION_ARCHITECTURE.md

Invariants:
- Generates a cryptographically random 256-bit (32-byte) master encryption key.
- Key material is NEVER hardcoded, derived from predictable seeds, or logged.
- Protects the master key at rest using Windows DPAPI (via KeyProtector).
- Strictly separated from per-contact encryption keys.
- Fails closed if platform key protection is unavailable or key is corrupted.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.security.crypto import KEY_LENGTH_BYTES, AES256GCMCipher
from src.security.dpapi import KeyProtectionError, KeyProtector, get_default_key_protector
from src.storage.paths import get_default_db_key_path

logger = logging.getLogger(__name__)

DB_KEY_ENTROPY = b"aegis:database_master_key:v1"


class DatabaseKeyError(RuntimeError):
    """Raised when database key generation, resolution, or unprotection fails."""


class DatabaseKeyManager:
    """Manages lifecycle, versioning, and protection of whole-database master keys."""

    def __init__(
        self,
        key_path: Optional[Path | str] = None,
        key_protector: Optional[KeyProtector] = None,
    ) -> None:
        self._key_path = Path(key_path) if key_path else get_default_db_key_path()
        self._manifest_path = self._key_path.parent / f"{self._key_path.stem}.manifest.json"
        self._protector = key_protector or get_default_key_protector()

    @property
    def key_path(self) -> Path:
        """Filesystem path to the protected master key file."""
        return self._key_path

    @property
    def manifest_path(self) -> Path:
        """Filesystem path to the key version manifest file."""
        return self._manifest_path

    def _read_manifest(self) -> Dict[str, Any]:
        """Read key version manifest from disk without exposing secrets."""
        if not self._manifest_path.is_file():
            return {
                "active_version": 1 if self._key_path.is_file() else 0,
                "versions": {},
            }
        try:
            loaded = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
            return {
                "active_version": 1 if self._key_path.is_file() else 0,
                "versions": {},
            }
        except Exception as exc:
            logger.warning("Failed to parse key manifest %s: %s", self._manifest_path, exc)
            return {
                "active_version": 1 if self._key_path.is_file() else 0,
                "versions": {},
            }

    def _write_manifest(self, data: Dict[str, Any]) -> None:
        """Atomically persist key version manifest to disk."""
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = self._manifest_path.parent
        with tempfile.NamedTemporaryFile(
            dir=temp_dir, mode="w", delete=False, encoding="utf-8"
        ) as tf:
            json.dump(data, tf, indent=2)
            temp_name = tf.name

        try:
            os.replace(temp_name, self._manifest_path)
        except Exception as exc:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise DatabaseKeyError(f"Failed to persist key manifest: {exc}") from exc

    def get_current_version(self) -> int:
        """Return the active key version integer (1-indexed)."""
        manifest = self._read_manifest()
        ver = int(manifest.get("active_version", 0))
        if ver == 0 and self._key_path.is_file():
            return 1
        return ver

    def get_version_key_path(self, version: int) -> Path:
        """Return filesystem path to a historical versioned key file."""
        return self._key_path.parent / f"{self._key_path.stem}.v{version}{self._key_path.suffix}"

    def get_or_create_key(self) -> bytes:
        """Retrieve existing master database key or generate and protect a new one.

        Returns:
            Raw 32-byte cryptographic key for SQLCipher PRAGMA key.

        Raises:
            DatabaseKeyError: If key protection or unprotection fails.
        """
        existing = self.get_key()
        if existing is not None:
            return existing

        # Generate fresh random 256-bit key
        raw_key = AES256GCMCipher.generate_random_key()
        try:
            protected_blob = self._protector.protect(raw_key, optional_entropy=DB_KEY_ENTROPY)
        except KeyProtectionError as exc:
            raise DatabaseKeyError(f"Failed to protect new database key with DPAPI: {exc}") from exc

        # Ensure parent directory exists
        self._key_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write via temporary file
        temp_dir = self._key_path.parent
        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as tf:
            tf.write(protected_blob)
            temp_name = tf.name

        try:
            os.replace(temp_name, self._key_path)
        except Exception as exc:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise DatabaseKeyError(f"Failed to persist protected database key: {exc}") from exc

        # Initialize manifest with version 1
        now_iso = datetime.now(timezone.utc).isoformat()
        manifest = {
            "active_version": 1,
            "versions": {
                "1": {
                    "filename": self._key_path.name,
                    "created_at": now_iso,
                }
            },
        }
        self._write_manifest(manifest)

        logger.info(
            "Generated and protected new database master key at %s (version 1)", self._key_path
        )
        return raw_key

    def get_key(self, version: Optional[int] = None) -> Optional[bytes]:
        """Retrieve and unprotect the master database key.

        Args:
            version: Optional 1-indexed version number. If None, retrieves the active key.
                     If specified, retrieves the historical key archived for that version.

        Returns:
            Raw 32-byte cryptographic key, or None if the key file does not exist.

        Raises:
            DatabaseKeyError: If key unprotection fails or key length is invalid.
        """
        if version is not None:
            current_ver = self.get_current_version()
            if version == current_ver:
                target_file = self._key_path
            else:
                target_file = self.get_version_key_path(version)
        else:
            target_file = self._key_path

        if not target_file.is_file():
            return None

        try:
            protected_blob = target_file.read_bytes()
        except Exception as exc:
            raise DatabaseKeyError(
                f"Failed to read protected key file '{target_file}': {exc}"
            ) from exc

        try:
            raw_key = self._protector.unprotect(protected_blob, optional_entropy=DB_KEY_ENTROPY)
        except KeyProtectionError as exc:
            raise DatabaseKeyError(
                f"Failed to unprotect database key at '{target_file}': {exc}"
            ) from exc

        if len(raw_key) != KEY_LENGTH_BYTES:
            raise DatabaseKeyError(f"Unprotected database key has invalid length: {len(raw_key)}")

        return raw_key

    def archive_current_key(self, version: int) -> Path:
        """Archive the current protected key file under a versioned historical filename.

        Args:
            version: Version number for the current key before rotation.

        Returns:
            Path to the newly archived versioned key file.
        """
        if not self._key_path.is_file():
            raise DatabaseKeyError("Cannot archive non-existent database key.")

        archived_path = self.get_version_key_path(version)
        blob = self._key_path.read_bytes()

        temp_dir = self._key_path.parent
        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as tf:
            tf.write(blob)
            temp_name = tf.name

        try:
            os.replace(temp_name, archived_path)
        except Exception as exc:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise DatabaseKeyError(f"Failed to archive version {version} key: {exc}") from exc

        # Update manifest
        manifest = self._read_manifest()
        versions = manifest.setdefault("versions", {})
        ver_info = versions.setdefault(str(version), {})
        ver_info["filename"] = archived_path.name
        ver_info["archived_at"] = datetime.now(timezone.utc).isoformat()
        self._write_manifest(manifest)

        logger.info("Archived database master key version %d to %s", version, archived_path)
        return archived_path

    def commit_new_key(self, new_key: bytes, new_version: int) -> None:
        """Atomically protect and commit a new master key as the active key file.

        Args:
            new_key: Raw 32-byte cryptographic key.
            new_version: Incremented version number for the new key.
        """
        if len(new_key) != KEY_LENGTH_BYTES:
            raise DatabaseKeyError(f"New key must be exactly {KEY_LENGTH_BYTES} bytes.")

        try:
            protected_blob = self._protector.protect(new_key, optional_entropy=DB_KEY_ENTROPY)
        except KeyProtectionError as exc:
            raise DatabaseKeyError(f"Failed to protect new database key with DPAPI: {exc}") from exc

        temp_dir = self._key_path.parent
        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as tf:
            tf.write(protected_blob)
            temp_name = tf.name

        try:
            os.replace(temp_name, self._key_path)
        except Exception as exc:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise DatabaseKeyError(f"Failed to persist new database key: {exc}") from exc

        # Update manifest active version
        manifest = self._read_manifest()
        manifest["active_version"] = new_version
        versions = manifest.setdefault("versions", {})
        versions[str(new_version)] = {
            "filename": self._key_path.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_manifest(manifest)
        logger.info(
            "Committed new active database master key at %s (version %d)",
            self._key_path,
            new_version,
        )

    def rollback_key(self, archived_version: int) -> None:
        """Rollback active master key to a previously archived version."""
        archived_path = self.get_version_key_path(archived_version)
        if not archived_path.is_file():
            raise DatabaseKeyError(
                f"Cannot rollback to non-existent version {archived_version} at '{archived_path}'."
            )

        blob = archived_path.read_bytes()
        temp_dir = self._key_path.parent
        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as tf:
            tf.write(blob)
            temp_name = tf.name

        try:
            os.replace(temp_name, self._key_path)
        except Exception as exc:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise DatabaseKeyError(f"Failed to rollback active key file: {exc}") from exc

        manifest = self._read_manifest()
        manifest["active_version"] = archived_version
        self._write_manifest(manifest)
        logger.info("Rolled back active database master key to version %d", archived_version)

    def get_version_history(self) -> List[Dict[str, Any]]:
        """Return non-sensitive version history of the database master key."""
        manifest = self._read_manifest()
        versions = manifest.get("versions", {})
        result = []
        for ver_str, info in sorted(versions.items(), key=lambda x: int(x[0])):
            result.append(
                {
                    "version": int(ver_str),
                    "filename": info.get("filename"),
                    "created_at": info.get("created_at"),
                    "archived_at": info.get("archived_at"),
                    "is_active": int(ver_str) == manifest.get("active_version"),
                }
            )
        return result

    def rotate_key(
        self,
        new_key: Optional[bytes] = None,
        allow_uncoordinated: bool = False,
        db_path: Optional[Path | str] = None,
    ) -> Tuple[bytes, bytes]:
        """Rotate the master database key.

        IMPORTANT: Calling this method without database coordination desynchronizes
        the database from the key file (Finding SEC-038-02).
        If `db_path` is provided, this method delegates to `rotate_database_key()`
        which rekeys the database, archives historical keys, and verifies integrity.
        If `allow_uncoordinated` is True, it allows rotating only the key file
        (strictly for isolated key-management unit tests).

        Raises:
            DatabaseKeyError: If called without db_path and allow_uncoordinated is False.
        """
        if db_path is not None:
            from src.security.database_key_rotation import rotate_database_key

            res = rotate_database_key(db_path, key_manager=self, new_key=new_key)
            return res["old_key"], res["new_key"]

        if not allow_uncoordinated:
            raise DatabaseKeyError(
                "Uncoordinated key rotation is prohibited because it desynchronizes the database "
                "from the key file (Finding SEC-038-02). Use rotate_database_key(db_path, mgr) "
                "or pass db_path to coordinate database rekeying, or set allow_uncoordinated=True "
                "strictly for isolated key-only unit tests."
            )

        old_key = self.get_key()
        if old_key is None:
            raise DatabaseKeyError("Cannot rotate non-existent database key.")

        fresh_key = new_key or AES256GCMCipher.generate_random_key()
        if len(fresh_key) != KEY_LENGTH_BYTES:
            raise DatabaseKeyError("New key must be exactly 32 bytes.")

        current_ver = self.get_current_version()
        self.archive_current_key(current_ver)
        self.commit_new_key(fresh_key, new_version=current_ver + 1)
        logger.info(
            "Rotated database master key at %s to version %d (uncoordinated mode)",
            self._key_path,
            current_ver + 1,
        )
        return old_key, fresh_key

    def destroy_key(self, destroy_history: bool = True) -> bool:
        """Cryptographically erase the database master key file and optional history.

        Args:
            destroy_history: If True, also erases all historical versioned key files and manifest.

        Returns:
            True if key file was found and removed, False otherwise.
        """
        found = False
        if self._key_path.is_file():
            try:
                self._key_path.unlink()
                found = True
            except Exception as exc:
                raise DatabaseKeyError(f"Failed to delete database key file: {exc}") from exc

        if destroy_history:
            # Erase versioned keys
            manifest = self._read_manifest()
            for ver_info in manifest.get("versions", {}).values():
                fn = ver_info.get("filename")
                if fn:
                    hist_file = self._key_path.parent / fn
                    if hist_file.is_file():
                        try:
                            hist_file.unlink()
                        except Exception:
                            pass
            if self._manifest_path.is_file():
                try:
                    self._manifest_path.unlink()
                except Exception:
                    pass

        if found:
            logger.info("Cryptographic erasure executed for database master key")
        return found
