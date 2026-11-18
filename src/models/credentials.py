"""SAGE Model Gateway -- Encrypted Credential & API Key Store.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13

Encrypts model API keys at rest using AES-256-GCM backed by platform DPAPI / KeyProtector.
Never exposes raw credentials in logs, memory, or ordinary configuration entries.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from src.models.errors import redact_secrets_from_text
from src.security.crypto import AES256GCMCipher
from src.security.dpapi import get_default_key_protector
from src.storage.database import transaction
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ModelCredentialStore:
    """Secure encrypted storage for AI model provider API keys and tokens."""

    TABLE_NAME = "model_credentials"

    def __init__(self, conn: sqlite3.Connection, encryption_key: Optional[bytes] = None) -> None:
        self._conn = conn
        self._protector = get_default_key_protector()
        if encryption_key is not None:
            if len(encryption_key) != 32:
                raise ValueError("Encryption key must be exactly 32 bytes (256 bits).")
            self._key = encryption_key
        else:
            # Deterministic persistent key derivation protected by DPAPI
            master_seed = b"SAGE_MODEL_GATEWAY_V1_KEY_SEED__"
            self._key = self._protector.protect(master_seed)[:32]
            if len(self._key) < 32:
                self._key = (self._key * 2)[:32]
        self._cipher = AES256GCMCipher(self._key)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create credentials table if it does not exist."""
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
            provider_id TEXT PRIMARY KEY,
            ciphertext BLOB NOT NULL,
            nonce BLOB NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        try:
            self._conn.executescript(sql)
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("Credential schema warning: %s", exc)

    def set_credential(self, provider_id: str, api_key: str) -> None:
        """Encrypt and store API key for a provider."""
        if not api_key or not api_key.strip():
            raise ValueError("API key must not be empty.")

        plaintext = api_key.strip().encode("utf-8")
        aad = provider_id.encode("utf-8")
        ciphertext, nonce = self._cipher.encrypt(plaintext, aad=aad)
        now_str = _utc_now_iso()

        sql = f"""
        INSERT INTO {self.TABLE_NAME} (provider_id, ciphertext, nonce, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(provider_id) DO UPDATE SET
            ciphertext = excluded.ciphertext,
            nonce = excluded.nonce,
            updated_at = excluded.updated_at;
        """
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (provider_id, ciphertext, nonce, now_str))

    def get_credential(self, provider_id: str) -> Optional[str]:
        """Decrypt and retrieve the API key for a provider."""
        sql = f"SELECT ciphertext, nonce FROM {self.TABLE_NAME} WHERE provider_id = ?;"
        cursor = self._conn.cursor()
        cursor.execute(sql, (provider_id,))
        row = cursor.fetchone()
        if not row:
            return None

        ciphertext, nonce = row
        aad = provider_id.encode("utf-8")
        try:
            decrypted = self._cipher.decrypt(ciphertext, nonce, aad=aad)
            return decrypted.decode("utf-8")
        except Exception as exc:
            logger.error("Failed to decrypt credential for provider %s: %s", provider_id, exc)
            return None

    def has_credential(self, provider_id: str) -> bool:
        """Check whether credentials exist for a provider without decrypting."""
        sql = f"SELECT 1 FROM {self.TABLE_NAME} WHERE provider_id = ?;"
        cursor = self._conn.cursor()
        cursor.execute(sql, (provider_id,))
        return cursor.fetchone() is not None

    def delete_credential(self, provider_id: str) -> bool:
        """Delete credentials for a provider."""
        sql = f"DELETE FROM {self.TABLE_NAME} WHERE provider_id = ?;"
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (provider_id,))
            return cursor.rowcount > 0

    def list_configured_providers(self) -> List[str]:
        """List provider IDs that have registered credentials."""
        sql = f"SELECT provider_id FROM {self.TABLE_NAME} ORDER BY provider_id ASC;"
        cursor = self._conn.cursor()
        cursor.execute(sql)
        return [r[0] for r in cursor.fetchall()]
