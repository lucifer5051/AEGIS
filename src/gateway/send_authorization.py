"""SAGE Remote Control Gateway -- Send Authorization Models and Errors.

Canonical Reference:
    AEGIS Prompt 036 / Phase 9C
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Security Invariant:
    SendAuthorization may only be instantiated inside this file and test files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from src.gateway.errors import GatewayError


class AuthorizationMismatchError(GatewayError):
    """Raised when outbound message parameters do not match SendAuthorization."""


class TargetNotAllowedError(GatewayError):
    """Raised when message recipient is not allowed by policy or bridge allowlist."""


class RateLimitedError(GatewayError):
    """Raised when outbound sending is rate-limited by the bridge or gateway."""


class SidecarTimeoutError(GatewayError):
    """Raised when the sidecar fails to acknowledge an outbound send within the timeout."""


@dataclass(frozen=True)
class SendAuthorization:
    """Cryptographically-grounded, immutable authorization token for an outbound send.

    Security Invariant:
        May only be instantiated inside src/gateway/send_authorization.py and test files.
    """

    approval_id: str
    recipient_jid: str
    text_sha256: str


def create_send_authorization(approval_id: str, recipient_jid: str, text: str) -> SendAuthorization:
    """Construct a SendAuthorization token by hashing message text."""
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return SendAuthorization(
        approval_id=approval_id,
        recipient_jid=recipient_jid,
        text_sha256=text_sha,
    )
