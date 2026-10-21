"""SAGE Platform Key Protection Subsystem -- Windows DPAPI & KeyProtector Interface.

Canonical Reference:
    AEGIS Prompt 036 (Phase 2.B)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Invariants:
- Never log plaintext keys or key material.
- Uses Windows DPAPI (CryptProtectData / CryptUnprotectData) under current user context.
- Fails closed if platform key protection is unavailable.
- Pluggable interface for future OS platforms (Linux keyutils / macOS Keychain).
"""

from __future__ import annotations

import abc
import ctypes
import logging
import os
import sys
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)


class KeyProtectionError(RuntimeError):
    """Raised when key wrapping or unwrapping fails."""


class KeyProtector(abc.ABC):
    """Abstract interface for protecting cryptographic keys at rest."""

    @abc.abstractmethod
    def protect(self, plaintext_key: bytes, optional_entropy: Optional[bytes] = None) -> bytes:
        """Wrap and protect a raw cryptographic key into an encrypted blob."""

    @abc.abstractmethod
    def unprotect(self, protected_blob: bytes, optional_entropy: Optional[bytes] = None) -> bytes:
        """Unwrap and decrypt a protected key blob back to the raw key."""


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class WindowsDPAPIKeyProtector(KeyProtector):
    """Windows Data Protection API (DPAPI) implementation using Crypt32."""

    def __init__(self, description: str = "AEGIS_CONTACT_DEK") -> None:
        if sys.platform != "win32":
            raise KeyProtectionError(
                f"WindowsDPAPIKeyProtector is only supported on Windows (current: {sys.platform})"
            )
        self._description = description
        try:
            self._crypt32 = ctypes.windll.crypt32
            self._kernel32 = ctypes.windll.kernel32
        except Exception as exc:
            raise KeyProtectionError(f"Failed to load Windows Crypt32 library: {exc}") from exc

    def protect(self, plaintext_key: bytes, optional_entropy: Optional[bytes] = None) -> bytes:
        """Protect a raw key using CryptProtectData with current user credentials."""
        if not plaintext_key:
            raise KeyProtectionError("Cannot protect empty key material")

        blob_in = _DATA_BLOB(
            len(plaintext_key),
            ctypes.cast(
                ctypes.create_string_buffer(plaintext_key, len(plaintext_key)),
                ctypes.POINTER(ctypes.c_byte),
            ),
        )
        blob_out = _DATA_BLOB()

        p_entropy = None
        blob_entropy = None
        if optional_entropy:
            blob_entropy = _DATA_BLOB(
                len(optional_entropy),
                ctypes.cast(
                    ctypes.create_string_buffer(optional_entropy, len(optional_entropy)),
                    ctypes.POINTER(ctypes.c_byte),
                ),
            )
            p_entropy = ctypes.byref(blob_entropy)

        # CRYPTPROTECT_UI_FORBIDDEN = 0x01
        flags = 0x01
        res = self._crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            self._description,
            p_entropy,
            None,
            None,
            flags,
            ctypes.byref(blob_out),
        )

        if not res:
            err = ctypes.GetLastError()
            raise KeyProtectionError(f"CryptProtectData failed with Windows error code {err}")

        try:
            protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            return bytes(protected)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)

    def unprotect(self, protected_blob: bytes, optional_entropy: Optional[bytes] = None) -> bytes:
        """Unprotect a raw key using CryptUnprotectData."""
        if not protected_blob:
            raise KeyProtectionError("Cannot unprotect empty blob")

        blob_in = _DATA_BLOB(
            len(protected_blob),
            ctypes.cast(
                ctypes.create_string_buffer(protected_blob, len(protected_blob)),
                ctypes.POINTER(ctypes.c_byte),
            ),
        )
        blob_out = _DATA_BLOB()

        p_entropy = None
        blob_entropy = None
        if optional_entropy:
            blob_entropy = _DATA_BLOB(
                len(optional_entropy),
                ctypes.cast(
                    ctypes.create_string_buffer(optional_entropy, len(optional_entropy)),
                    ctypes.POINTER(ctypes.c_byte),
                ),
            )
            p_entropy = ctypes.byref(blob_entropy)

        flags = 0x01
        res = self._crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            p_entropy,
            None,
            None,
            flags,
            ctypes.byref(blob_out),
        )

        if not res:
            err = ctypes.GetLastError()
            raise KeyProtectionError(f"CryptUnprotectData failed with Windows error code {err}")

        try:
            plaintext = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            return bytes(plaintext)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)


class SoftwareKeyProtector(KeyProtector):
    """Software-based key protector wrapping keys using a supplied 32-byte master key.

    Used for testing or platforms where DPAPI is unavailable. Fails closed if master key is invalid.
    """

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise KeyProtectionError("SoftwareKeyProtector requires a 32-byte master key")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._aesgcm = AESGCM(master_key)

    def protect(self, plaintext_key: bytes, optional_entropy: Optional[bytes] = None) -> bytes:
        if not plaintext_key:
            raise KeyProtectionError("Cannot protect empty key material")
        nonce = os.urandom(12)
        aad = optional_entropy or b"aegis:software_key_protector"
        ciphertext = self._aesgcm.encrypt(nonce, plaintext_key, aad)
        return nonce + ciphertext

    def unprotect(self, protected_blob: bytes, optional_entropy: Optional[bytes] = None) -> bytes:
        if len(protected_blob) < 28:
            raise KeyProtectionError("Protected blob is too short")
        nonce = protected_blob[:12]
        ciphertext = protected_blob[12:]
        aad = optional_entropy or b"aegis:software_key_protector"
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, aad)
        except Exception as exc:
            raise KeyProtectionError(f"Software key unprotection failed: {exc}") from exc


def get_default_key_protector() -> KeyProtector:
    """Return default platform key protector.

    Uses Windows DPAPI on Windows, failing closed on unsupported platforms.
    """
    if sys.platform == "win32":
        return WindowsDPAPIKeyProtector()
    raise KeyProtectionError(
        f"No native platform KeyProtector available for {sys.platform}. "
        "Failing closed to prevent plaintext key exposure."
    )
