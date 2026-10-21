"""SAGE Authenticated Encryption Subsystem -- AES-256-GCM.

Canonical Reference:
    AEGIS Prompt 036 (Phase 2.A)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Invariants:
- Uses vetted AES-256-GCM AEAD construction from `cryptography.hazmat.primitives.ciphers.aead`.
- Requires 256-bit (32-byte) cryptographically random keys.
- Generates a unique 96-bit (12-byte) CSPRNG nonce (`os.urandom(12)`) for every encryption.
- Authenticates associated data (AAD) including contact identity, record ID, and version.
- Rejects any tampering or authentication tag mismatch without returning partial plaintext.
- Never logs keys, plaintexts, or decrypted memory.
"""

from __future__ import annotations

import logging
import os
from typing import Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

KEY_LENGTH_BYTES = 32
NONCE_LENGTH_BYTES = 12


class CryptoError(Exception):
    """Base exception for cryptographic operations."""


class DecryptionError(CryptoError):
    """Raised when ciphertext authentication or decryption fails."""


class AES256GCMCipher:
    """Authenticated encryption and decryption using AES-256-GCM."""

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)):
            raise CryptoError("Key must be bytes")
        if len(key) != KEY_LENGTH_BYTES:
            raise CryptoError(f"AES-256-GCM requires a 32-byte (256-bit) key, got {len(key)} bytes")
        self._key = bytes(key)
        self._cipher = AESGCM(self._key)

    @classmethod
    def generate_random_key(cls) -> bytes:
        """Generate a cryptographically random 256-bit key."""
        return AESGCM.generate_key(bit_length=256)

    def encrypt(self, plaintext: bytes, aad: bytes) -> Tuple[bytes, bytes]:
        """Encrypt plaintext using AES-256-GCM with a fresh 12-byte nonce.

        Args:
            plaintext: Raw payload bytes to encrypt.
            aad: Associated authenticated data bytes (e.g. contact JID + record ID).

        Returns:
            Tuple of (ciphertext_with_tag, nonce).
        """
        if not isinstance(plaintext, (bytes, bytearray)):
            raise CryptoError("Plaintext must be bytes")
        if not isinstance(aad, (bytes, bytearray)):
            raise CryptoError("Associated authenticated data (AAD) must be bytes")

        nonce = os.urandom(NONCE_LENGTH_BYTES)
        try:
            ciphertext = self._cipher.encrypt(nonce, bytes(plaintext), bytes(aad))
            return ciphertext, nonce
        except Exception as exc:
            raise CryptoError(f"Encryption failed: {exc}") from exc

    def decrypt(self, ciphertext: bytes, nonce: bytes, aad: bytes) -> bytes:
        """Decrypt and verify ciphertext using AES-256-GCM.

        Args:
            ciphertext: Encrypted payload bytes with authentication tag.
            nonce: 12-byte nonce used during encryption.
            aad: Associated authenticated data bytes.

        Returns:
            Decrypted plaintext bytes.

        Raises:
            DecryptionError: If authentication tag check fails, nonce is invalid,
                or ciphertext has been tampered with.
        """
        if not isinstance(ciphertext, (bytes, bytearray)):
            raise DecryptionError("Ciphertext must be bytes")
        if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != NONCE_LENGTH_BYTES:
            got_desc = len(nonce) if isinstance(nonce, (bytes, bytearray)) else "non-bytes"
            raise DecryptionError(
                f"Nonce must be exactly {NONCE_LENGTH_BYTES} bytes, got {got_desc}"
            )
        if not isinstance(aad, (bytes, bytearray)):
            raise DecryptionError("Associated authenticated data (AAD) must be bytes")

        try:
            plaintext = self._cipher.decrypt(bytes(nonce), bytes(ciphertext), bytes(aad))
            return plaintext
        except InvalidTag as exc:
            logger.warning("Decryption failed: AEAD authentication tag mismatch or tampered data")
            raise DecryptionError(
                "Decryption failed: authentication tag verification failed"
            ) from exc
        except Exception as exc:
            logger.warning("Decryption failed: %s", type(exc).__name__)
            raise DecryptionError(f"Decryption failed: {exc}") from exc
