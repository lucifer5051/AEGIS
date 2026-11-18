"""SAGE Configuration & Policy Persistence Store.

Canonical Reference:
    PROMPT 069: SAGE Configuration & Policy System

Features:
- Encrypted SQLite backed storage for configuration layers and custom modes.
- Zero-secret enforcement before any write.
- Atomic transactions (BEGIN IMMEDIATE).
- Versioned history snapshots for audit trails and safe rollback.
- Recovery and migration compatibility without altering base migrations.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.config.policy_models import ConfigurationLayer, CustomMode
from src.storage.database import transaction
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class PolicyConfigStore:
    """Persistent storage engine for SAGE configurations, modes, and version history."""

    TABLE_ENTRIES = "configuration_entries"
    TABLE_MODES = "configuration_modes"
    TABLE_HISTORY = "configuration_history"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create auxiliary SQLite tables if they do not already exist."""
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.TABLE_ENTRIES} (
            layer TEXT NOT NULL,
            config_key TEXT NOT NULL,
            config_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT 'system',
            PRIMARY KEY (layer, config_key)
        );

        CREATE TABLE IF NOT EXISTS {self.TABLE_MODES} (
            mode_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            mode_json TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cfg_modes_name ON {self.TABLE_MODES}(name);

        CREATE TABLE IF NOT EXISTS {self.TABLE_HISTORY} (
            history_id TEXT PRIMARY KEY,
            version_number INTEGER NOT NULL,
            layer TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cfg_history_ver ON {self.TABLE_HISTORY}(version_number);
        """
        try:
            self._conn.executescript(sql)
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("Configuration schema creation warning: %s", exc)

    # =========================================================================
    # LAYER CONFIGURATION STORAGE
    # =========================================================================

    def save_layer_config(
        self,
        layer: ConfigurationLayer,
        config_data: Dict[str, Any],
        reason: str = "Configuration update",
        actor: str = "system",
    ) -> int:
        """Atomically persist a layer configuration and capture a history snapshot."""
        # Enforce zero secrets
        payload_str = json.dumps(config_data, sort_keys=True)
        _check_no_secrets(payload_str)
        _check_no_secrets(reason)

        now = _utc_now_iso()

        with transaction(self._conn, immediate=True) as cur:
            # 1. Update or replace entries for this layer
            cur.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_ENTRIES} (
                    layer, config_key, config_json, updated_at, updated_by
                ) VALUES (?, 'root', ?, ?, ?);
                """,
                (layer.value, payload_str, now, actor),
            )

            # 2. Get next version number
            cur.execute(f"SELECT COALESCE(MAX(version_number), 0) + 1 FROM {self.TABLE_HISTORY};")
            next_ver = cur.fetchone()[0]

            # 3. Create history snapshot
            history_id = str(uuid4())
            cur.execute(
                f"""
                INSERT INTO {self.TABLE_HISTORY} (
                    history_id, version_number, layer, snapshot_json, created_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (history_id, next_ver, layer.value, payload_str, now, reason),
            )

            return next_ver

    def load_layer_config(self, layer: ConfigurationLayer) -> Dict[str, Any]:
        """Load the persisted configuration dictionary for a specific layer."""
        cursor = self._conn.execute(
            f"SELECT config_json FROM {self.TABLE_ENTRIES} WHERE layer = ? AND config_key = 'root';",
            (layer.value,),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return {}
        try:
            return json.loads(row[0])
        except Exception as exc:
            logger.error("Failed to parse persisted configuration for layer %s: %s", layer.value, exc)
            return {}

    # =========================================================================
    # CUSTOM MODES PERSISTENCE
    # =========================================================================

    def save_mode(self, mode: CustomMode) -> str:
        """Store or update a CustomMode record."""
        mode_json = json.dumps(mode.model_dump(), sort_keys=True)
        _check_no_secrets(mode_json)

        now = _utc_now_iso()
        with transaction(self._conn, immediate=True) as cur:
            if mode.is_default:
                # Clear existing defaults
                cur.execute(f"UPDATE {self.TABLE_MODES} SET is_default = 0;")

            cur.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_MODES} (
                    mode_id, name, mode_json, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (mode.mode_id, mode.name, mode_json, 1 if mode.is_default else 0, mode.created_at, now),
            )
            return mode.mode_id

    def get_mode(self, mode_id_or_name: str) -> Optional[CustomMode]:
        """Retrieve a CustomMode by UUID or unique name."""
        cursor = self._conn.execute(
            f"SELECT mode_json FROM {self.TABLE_MODES} WHERE mode_id = ? OR name = ? LIMIT 1;",
            (mode_id_or_name, mode_id_or_name),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return None
        data = json.loads(row[0])
        return CustomMode.model_validate(data)

    def list_modes(self) -> List[CustomMode]:
        """List all registered custom modes."""
        cursor = self._conn.execute(
            f"SELECT mode_json FROM {self.TABLE_MODES} ORDER BY is_default DESC, name ASC;"
        )
        results = []
        for row in cursor.fetchall():
            try:
                results.append(CustomMode.model_validate(json.loads(row[0])))
            except Exception as exc:
                logger.warning("Failed to deserialize mode record: %s", exc)
        return results

    def delete_mode(self, mode_id_or_name: str) -> bool:
        """Delete a custom mode by ID or name."""
        with transaction(self._conn, immediate=True) as cur:
            res = cur.execute(
                f"DELETE FROM {self.TABLE_MODES} WHERE mode_id = ? OR name = ?;",
                (mode_id_or_name, mode_id_or_name),
            )
            return res.rowcount > 0

    def get_default_mode(self) -> Optional[CustomMode]:
        """Retrieve the active default custom mode, if set."""
        cursor = self._conn.execute(
            f"SELECT mode_json FROM {self.TABLE_MODES} WHERE is_default = 1 LIMIT 1;"
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return None
        return CustomMode.model_validate(json.loads(row[0]))

    # =========================================================================
    # HISTORY & ROLLBACK
    # =========================================================================

    def list_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent configuration history versions."""
        cursor = self._conn.execute(
            f"""
            SELECT version_number, layer, created_at, reason
            FROM {self.TABLE_HISTORY}
            ORDER BY version_number DESC
            LIMIT ?;
            """,
            (limit,),
        )
        return [
            {
                "version_number": r[0],
                "layer": r[1],
                "created_at": r[2],
                "reason": r[3],
            }
            for r in cursor.fetchall()
        ]

    def rollback_to_version(self, version_number: int) -> Dict[str, Any]:
        """Rollback configuration layer to a previously recorded valid snapshot."""
        cursor = self._conn.execute(
            f"""
            SELECT layer, snapshot_json
            FROM {self.TABLE_HISTORY}
            WHERE version_number = ?
            LIMIT 1;
            """,
            (version_number,),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Configuration version {version_number} not found in history.")

        layer_str, snapshot_str = row[0], row[1]
        data = json.loads(snapshot_str)
        now = _utc_now_iso()

        with transaction(self._conn, immediate=True) as cur:
            # Revert layer
            cur.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_ENTRIES} (
                    layer, config_key, config_json, updated_at, updated_by
                ) VALUES (?, 'root', ?, ?, 'rollback');
                """,
                (layer_str, snapshot_str, now),
            )

            # Record rollback in history as new version
            cur.execute(f"SELECT COALESCE(MAX(version_number), 0) + 1 FROM {self.TABLE_HISTORY};")
            new_ver = cur.fetchone()[0]
            cur.execute(
                f"""
                INSERT INTO {self.TABLE_HISTORY} (
                    history_id, version_number, layer, snapshot_json, created_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    str(uuid4()),
                    new_ver,
                    layer_str,
                    snapshot_str,
                    now,
                    f"Rollback to version {version_number}",
                ),
            )

        return {
            "reverted_layer": layer_str,
            "target_version": version_number,
            "new_version": new_ver,
            "config": data,
        }
