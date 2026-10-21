"""SAGE Remote Control Gateway -- Error Hierarchy.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- All exceptions fail closed.
- Zero secrets or sensitive credentials permitted in exception messages.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class GatewayError(Exception):
    """Base exception for all SAGE Gateway errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ChannelConfigError(GatewayError):
    """Configuration missing, invalid, or token environment variable unresolved."""


class AuthenticationError(GatewayError):
    """Sender authentication failed (unapproved numeric user ID, spoofed identity)."""


class UpdateValidationError(GatewayError):
    """Inbound update is malformed, oversized, from a bot, or non-private chat."""


class RateLimitExceededError(GatewayError):
    """Sender has exceeded the configured message rate limit."""


class DuplicateUpdateError(GatewayError):
    """Update ID has already been recorded and processed."""


class TransportError(GatewayError):
    """Channel transport or network layer failed (timeout, connection error)."""


class CommandExecutionError(GatewayError):
    """Error during command parsing, routing, or task dispatch."""
