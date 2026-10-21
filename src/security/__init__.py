"""SAGE Security & Cryptography Subsystem.

Canonical Reference:
    AEGIS Prompt 036: Secure Contact Memory & WhatsApp Receive-Only Integration
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md
"""

from __future__ import annotations

from src.security.contact_key_store import ContactIdentityError, ContactKeyStore
from src.security.crypto import AES256GCMCipher, CryptoError, DecryptionError
from src.security.database_key import DatabaseKeyError, DatabaseKeyManager
from src.security.database_key_rotation import (
    ExclusiveDatabaseLock,
    RotationError,
    RotationJournal,
    RotationLockError,
    RotationState,
    recover_interrupted_rotation,
    rotate_database_key,
)
from src.security.dpapi import KeyProtector, WindowsDPAPIKeyProtector

__all__ = [
    "AES256GCMCipher",
    "CryptoError",
    "DecryptionError",
    "KeyProtector",
    "WindowsDPAPIKeyProtector",
    "ContactKeyStore",
    "ContactIdentityError",
    "DatabaseKeyManager",
    "DatabaseKeyError",
    "rotate_database_key",
    "recover_interrupted_rotation",
    "ExclusiveDatabaseLock",
    "RotationJournal",
    "RotationState",
    "RotationError",
    "RotationLockError",
]
