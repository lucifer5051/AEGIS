"""SAGE Friday Communication Gateway -- Error Definitions.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol
"""

from __future__ import annotations

from typing import Optional

from src.models.errors import redact_secrets_from_text


class CommunicationError(Exception):
    """Base exception for all communication gateway errors."""

    def __init__(self, message: str, details: Optional[str] = None) -> None:
        safe_msg = redact_secrets_from_text(message)
        super().__init__(safe_msg)
        self.message = safe_msg
        self.details = redact_secrets_from_text(details) if details else None


class ChannelAuthenticationError(CommunicationError):
    """Raised when channel authentication (e.g. invalid bot token, signature mismatch) fails."""


class ChannelAuthorizationError(CommunicationError):
    """Raised when a sender or channel is denied access by policy or TrustCore."""


class UnauthorizedSenderError(ChannelAuthorizationError):
    """Raised when a sender is not recognized or lacks required permissions."""


class ChannelUnavailableError(CommunicationError):
    """Raised when an external messaging provider or transport is offline."""


class MessageValidationError(CommunicationError):
    """Raised when an incoming or outgoing message violates format or security constraints."""


class SidecarSecurityError(CommunicationError):
    """Raised when a sidecar IPC protocol violation, replay, or authentication failure occurs."""


class SidecarTimeoutError(CommunicationError):
    """Raised when a sidecar IPC request exceeds the timeout threshold."""


class AttachmentPolicyError(CommunicationError):
    """Raised when an attachment exceeds size limits or has a forbidden content type."""


class RateLimitExceededError(CommunicationError):
    """Raised when an actor or recipient exceeds messaging rate limits."""
