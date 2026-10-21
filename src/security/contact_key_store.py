"""SAGE Contact Key Management & Isolation Subsystem.

Canonical Reference:
    AEGIS Prompt 036 (Phase 2.A, 2.B, 2.C, 2.E)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Invariants:
- Generates cryptographically random 256-bit AES data-encryption keys (DEKs) per contact.
- Never derives keys from phone numbers, JIDs, names, timestamps, or predictable seeds.
- Protects DEKs at rest using Windows DPAPI. Plaintext keys are NEVER written to disk/logs.
- Binds keys strictly to verified contact JIDs (rejecting groups/broadcasts).
- Supports immediate key destruction for cryptographic erasure.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from src.security.crypto import KEY_LENGTH_BYTES, AES256GCMCipher
from src.security.dpapi import KeyProtector, get_default_key_protector

logger = logging.getLogger(__name__)

# Valid WhatsApp individual contact JID: digits followed by @s.whatsapp.net
_WHATSAPP_INDIVIDUAL_JID_RE = re.compile(r"^\d{7,15}@s\.whatsapp\.net$")


class ContactIdentityError(ValueError):
    """Raised when contact identity format, normalization, or validation fails."""


def normalize_and_validate_contact_jid(jid: str) -> str:
    """Normalize and validate a WhatsApp contact JID for security operations.

    Args:
        jid: Raw JID string from transport or caller.

    Returns:
        Canonical lowercase stripped JID.

    Raises:
        ContactIdentityError: If JID is a group, broadcast, malformed, or unauthorized format.
    """
    if not isinstance(jid, str) or not jid.strip():
        raise ContactIdentityError("Contact JID must be a non-empty string")

    clean = jid.strip().lower()

    # Reject groups and broadcasts before key or memory operations
    if clean.endswith("@g.us") or "@g.us" in clean or "group" in clean:
        raise ContactIdentityError(
            f"Unauthorized contact identity: group destinations ({clean}) are strictly prohibited."
        )

    if "@broadcast" in clean or clean.endswith("@broadcast"):
        raise ContactIdentityError(
            f"Unauthorized contact: broadcast destination ({clean}) is prohibited."
        )

    # If domain omitted, check if pure phone number and append standard WhatsApp domain
    if "@" not in clean and clean.isdigit() and 7 <= len(clean) <= 15:
        clean = f"{clean}@s.whatsapp.net"

    if not _WHATSAPP_INDIVIDUAL_JID_RE.match(clean):
        raise ContactIdentityError(
            f"Malformed contact JID '{clean}'. Must match phone_number@s.whatsapp.net."
        )

    return clean


class ContactKeyStore:
    """Secure store managing per-contact data encryption keys (DEKs)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        key_protector: Optional[KeyProtector] = None,
    ) -> None:
        self._conn = conn
        self._protector = key_protector or get_default_key_protector()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the protected contact_keys table if not present."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_keys (
                    contact_jid     TEXT PRIMARY KEY,
                    protected_dek   BLOB NOT NULL,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                """
            )

    def get_or_create_key(self, contact_jid: str) -> bytes:
        """Retrieve existing contact DEK or generate a new random 256-bit DEK.

        Args:
            contact_jid: Verified contact JID.

        Returns:
            Raw 32-byte data encryption key.
        """
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        existing_key = self.get_key(canonical_jid)
        if existing_key is not None:
            return existing_key

        # Generate fresh cryptographically random 256-bit key
        raw_dek = AES256GCMCipher.generate_random_key()
        entropy = canonical_jid.encode("utf-8")
        protected_dek = self._protector.protect(raw_dek, optional_entropy=entropy)

        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO contact_keys (contact_jid, protected_dek, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_jid) DO UPDATE SET
                    protected_dek = excluded.protected_dek,
                    updated_at = excluded.updated_at;
                """,
                (canonical_jid, protected_dek, now, now),
            )
        logger.info("Generated and protected new DEK for contact_jid=%s", canonical_jid)
        return raw_dek

    def get_key(self, contact_jid: str) -> Optional[bytes]:
        """Retrieve and unprotect the contact DEK for a verified contact JID.

        Args:
            contact_jid: Verified contact JID.

        Returns:
            Raw 32-byte DEK, or None if no key exists.
        """
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT protected_dek FROM contact_keys WHERE contact_jid = ?",
            (canonical_jid,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        protected_dek = row[0]
        entropy = canonical_jid.encode("utf-8")
        unprotected = self._protector.unprotect(protected_dek, optional_entropy=entropy)
        if len(unprotected) != KEY_LENGTH_BYTES:
            raise ContactIdentityError("Unprotected key has invalid length")
        return unprotected

    def has_key(self, contact_jid: str) -> bool:
        """Check whether a key exists for the given contact JID."""
        try:
            canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        except ContactIdentityError:
            return False

        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT 1 FROM contact_keys WHERE contact_jid = ?",
            (canonical_jid,),
        )
        return cursor.fetchone() is not None

    def destroy_key(self, contact_jid: str) -> bool:
        """Destroy the contact DEK from storage (Cryptographic Erasure).

        Once destroyed, any encrypted data previously written with this key
        becomes cryptographically inaccessible even if backup records exist.

        Args:
            contact_jid: Contact JID whose key is to be destroyed.

        Returns:
            True if a key record was found and deleted, False otherwise.
        """
        canonical_jid = normalize_and_validate_contact_jid(contact_jid)
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM contact_keys WHERE contact_jid = ?",
                (canonical_jid,),
            )
            deleted = cursor.rowcount > 0

        if deleted:
            logger.info("Cryptographic erasure executed for contact_jid=%s", canonical_jid)
        return deleted
