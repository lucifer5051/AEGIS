"""SAGE Encrypted Per-Contact Memory Store.

Canonical Reference:
    AEGIS Prompt 036 (Phase 2.A, 2.C, 2.D, 2.E)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Invariants:
- All queries strictly scoped by contact identity in storage layer (`WHERE contact_jid = ?`).
- Record bodies encrypted with contact's isolated AES-256-GCM data-encryption key (DEK).
- Unique 12-byte CSPRNG nonce generated per record encryption.
- AAD binds ciphertext to canonical contact JID, record ID, and version.
- Authentication failures fail closed immediately with zero partial plaintext.
- Never logs keys, plaintext message content, or decrypted private memory.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.security.contact_key_store import (
    ContactKeyStore,
    normalize_and_validate_contact_jid,
)
from src.security.crypto import AES256GCMCipher, DecryptionError
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "v1"


def _build_aad(contact_jid: str, record_id: str, record_type: str) -> bytes:
    """Build deterministic Associated Authenticated Data (AAD) for AES-256-GCM."""
    return f"aegis:contact_memory:{SCHEMA_VERSION}:{contact_jid}:{record_id}:{record_type}".encode(
        "utf-8"
    )


class EncryptedContactMemoryStore:
    """Storage layer providing encrypted per-contact memory isolation."""

    def __init__(self, conn: sqlite3.Connection, key_store: ContactKeyStore) -> None:
        self._conn = conn
        self._key_store = key_store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the encrypted contact memory tables and indexes."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_memory_records (
                    record_id       TEXT PRIMARY KEY,
                    contact_jid     TEXT NOT NULL,
                    record_type     TEXT NOT NULL,
                    ciphertext      BLOB NOT NULL,
                    nonce           BLOB NOT NULL,
                    aad_text        TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_contact_memory_lookup
                ON contact_memory_records (contact_jid, record_type);
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_contact_memory_created
                ON contact_memory_records (contact_jid, created_at);
                """
            )

    def store_memory(
        self,
        contact_jid: str,
        record_type: str,
        content: Dict[str, Any],
        record_id: Optional[str] = None,
    ) -> str:
        """Encrypt and persist a private memory record for an authorized contact.

        Args:
            contact_jid: Verified canonical contact JID.
            record_type: Semantic record category (e.g. 'preference', 'note', 'context').
            content: Arbitrary JSON-serializable dictionary.
            record_id: Optional unique record ID (generated if not provided).

        Returns:
            The stored record_id.
        """
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        rec_id = record_id or str(uuid4())

        # Serialize and check zero secrets invariant
        payload_str = json.dumps(content, sort_keys=True)
        _check_no_secrets(payload_str)
        payload_bytes = payload_str.encode("utf-8")

        # Retrieve or generate the contact's isolated DEK
        dek = self._key_store.get_or_create_key(canonical_jid)
        cipher = AES256GCMCipher(dek)

        aad_bytes = _build_aad(canonical_jid, rec_id, record_type)
        ciphertext, nonce = cipher.encrypt(payload_bytes, aad=aad_bytes)

        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO contact_memory_records (
                    record_id, contact_jid, record_type, ciphertext,
                    nonce, aad_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    nonce = excluded.nonce,
                    aad_text = excluded.aad_text,
                    updated_at = excluded.updated_at;
                """,
                (
                    rec_id,
                    canonical_jid,
                    record_type,
                    ciphertext,
                    nonce,
                    aad_bytes.decode("utf-8"),
                    now,
                    now,
                ),
            )
        logger.info(
            "Persisted encrypted record %s for contact_jid=%s (type=%s)",
            rec_id,
            canonical_jid,
            record_type,
        )
        return rec_id

    def retrieve_memory(
        self,
        contact_jid: str,
        record_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve and decrypt a memory record strictly scoped to the requesting contact.

        Args:
            contact_jid: Verified contact JID attempting retrieval.
            record_id: Unique record ID.

        Returns:
            Decrypted dictionary with record metadata, or None if not found.

        Raises:
            DecryptionError: If ciphertext has been tampered with or authentication tag fails.
            ContactIdentityError: If contact JID is invalid.
        """
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)

        cursor = self._conn.cursor()
        # SQL-level scoping: Contact A can NEVER query Contact B's row
        cursor.execute(
            """
            SELECT record_id, record_type, ciphertext, nonce, aad_text, created_at, updated_at
            FROM contact_memory_records
            WHERE contact_jid = ? AND record_id = ?
            """,
            (canonical_jid, record_id),
        )
        row = cursor.fetchone()
        if not row:
            return None

        rec_id, rec_type, ciphertext, nonce, aad_text, created_at, updated_at = row

        dek = self._key_store.get_key(canonical_jid)
        if dek is None:
            logger.warning(
                "Cannot decrypt record %s: no DEK found for contact_jid=%s",
                record_id,
                canonical_jid,
            )
            return None

        cipher = AES256GCMCipher(dek)
        aad_bytes = _build_aad(canonical_jid, rec_id, rec_type)
        try:
            plaintext = cipher.decrypt(ciphertext, nonce, aad=aad_bytes)
        except DecryptionError:
            logger.error(
                "Tampered or invalid memory record %s for contact %s", rec_id, canonical_jid
            )
            raise

        data = json.loads(plaintext.decode("utf-8"))
        return {
            "record_id": rec_id,
            "contact_jid": canonical_jid,
            "record_type": rec_type,
            "data": data,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def list_contact_memories(
        self,
        contact_jid: str,
        record_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Retrieve and decrypt all memories for a verified contact.

        Args:
            contact_jid: Verified contact JID.
            record_type: Optional filter by record type.
            limit: Maximum records to return.

        Returns:
            List of decrypted record dictionaries.
        """
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        dek = self._key_store.get_key(canonical_jid)
        if dek is None:
            return []

        cipher = AES256GCMCipher(dek)
        cursor = self._conn.cursor()

        if record_type:
            cursor.execute(
                """
                SELECT record_id, record_type, ciphertext, nonce, aad_text, created_at, updated_at
                FROM contact_memory_records
                WHERE contact_jid = ? AND record_type = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (canonical_jid, record_type, limit),
            )
        else:
            cursor.execute(
                """
                SELECT record_id, record_type, ciphertext, nonce, aad_text, created_at, updated_at
                FROM contact_memory_records
                WHERE contact_jid = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (canonical_jid, limit),
            )

        results: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            rec_id, rec_type, ciphertext, nonce, aad_text, created_at, updated_at = row
            aad_bytes = _build_aad(canonical_jid, rec_id, rec_type)
            try:
                plaintext = cipher.decrypt(ciphertext, nonce, aad=aad_bytes)
                data = json.loads(plaintext.decode("utf-8"))
                results.append(
                    {
                        "record_id": rec_id,
                        "contact_jid": canonical_jid,
                        "record_type": rec_type,
                        "data": data,
                        "created_at": created_at,
                        "updated_at": updated_at,
                    }
                )
            except DecryptionError:
                logger.error("Skipping corrupted/tampered memory record %s", rec_id)
                continue

        return results

    def delete_memory(self, contact_jid: str, record_id: str) -> bool:
        """Delete a single memory record belonging to a verified contact."""
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        with self._conn:
            cursor = self._conn.execute(
                """
                DELETE FROM contact_memory_records
                WHERE contact_jid = ? AND record_id = ?
                """,
                (canonical_jid, record_id),
            )
            return cursor.rowcount > 0

    def delete_all_contact_memories(self, contact_jid: str) -> int:
        """Delete all memory records belonging to a contact."""
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        with self._conn:
            cursor = self._conn.execute(
                """
                DELETE FROM contact_memory_records
                WHERE contact_jid = ?
                """,
                (canonical_jid,),
            )
            return cursor.rowcount

    def purge_contact_completely(self, contact_jid: str) -> Dict[str, Any]:
        """Perform comprehensive contact deletion and cryptographic erasure.

        1. Deletes all memory records belonging to the contact.
        2. Destroys the protected DEK from the key store.
        Even if backups of encrypted records exist on disk or cold storage,
        destroying the DEK renders the ciphertext permanently unrecoverable.

        Args:
            contact_jid: Verified contact JID.

        Returns:
            Dict summary of deletion results.
        """
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        with self._conn:
            deleted_records = self.delete_all_contact_memories(canonical_jid)
            key_destroyed = self._key_store.destroy_key(canonical_jid)

        logger.info(
            "Purged contact %s: %d records deleted, key_destroyed=%s",
            canonical_jid,
            deleted_records,
            key_destroyed,
        )
        return {
            "contact_jid": canonical_jid,
            "records_deleted": deleted_records,
            "key_destroyed": key_destroyed,
        }
