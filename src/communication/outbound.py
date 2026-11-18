"""SAGE Friday Communication Gateway -- Outbound Policy Enforcement.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol

Invariants:
- All outbound messages pass through a centralized policy layer.
- Proactive messages require explicit authorization.
- Zero-Secret Invariant: Every outbound message is scrubbed of credentials before transmission.
- Rate limits strictly enforced per recipient.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple

from src.communication.domain import (
    ChannelPlatform,
    OutgoingMessage,
)
from src.communication.errors import (
    ChannelAuthorizationError,
    MessageValidationError,
    RateLimitExceededError,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.models.errors import redact_secrets_from_text

logger = logging.getLogger(__name__)


class OutboundPolicyEngine:
    """Evaluates and sanitizes outbound communication before dispatch."""

    def __init__(
        self,
        allowed_recipients: Optional[Set[Tuple[ChannelPlatform, str]]] = None,
        proactive_enabled: bool = False,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        max_rate_per_minute: int = 30,
    ) -> None:
        self._allowed_recipients: Set[Tuple[ChannelPlatform, str]] = (
            set(allowed_recipients) if allowed_recipients else set()
        )
        self._proactive_enabled = proactive_enabled
        self._limiter = rate_limiter or SlidingWindowRateLimiter()
        self._max_rate = max_rate_per_minute

    def allow_recipient(self, channel: ChannelPlatform, recipient_id: str) -> None:
        """Explicitly register an authorized outbound recipient destination."""
        self._allowed_recipients.add((channel, str(recipient_id).strip()))

    def validate_and_sanitize(
        self,
        outgoing: OutgoingMessage,
        is_proactive: bool = False,
    ) -> OutgoingMessage:
        """Validate destination, check rate limits, and sanitize content.

        Raises:
            ChannelAuthorizationError: If recipient is not authorized or proactive sends are disabled.
            RateLimitExceededError: If transmission rate is exceeded.
            MessageValidationError: If message is empty.
        """
        dest = str(outgoing.destination).strip()
        if not dest:
            raise MessageValidationError("Outbound destination is empty.")

        if not outgoing.content or not outgoing.content.strip():
            raise MessageValidationError("Outbound message content is empty.")

        # Recipient authorization check
        if self._allowed_recipients and (outgoing.channel, dest) not in self._allowed_recipients:
            raise ChannelAuthorizationError(
                f"Outbound recipient '{dest}' on {outgoing.channel.value} is not in allowed recipients."
            )

        # Proactive authorization check
        if is_proactive and not self._proactive_enabled:
            raise ChannelAuthorizationError(
                f"Proactive/scheduled messaging is disabled on channel {outgoing.channel.value}."
            )

        # Rate limiting check
        rate_key = f"{outgoing.channel.value}:{dest}"
        if not self._limiter.record_and_check(rate_key, limit=self._max_rate, window_seconds=60.0):
            raise RateLimitExceededError(
                f"Rate limit exceeded ({self._max_rate}/min) for recipient {dest} on {outgoing.channel.value}."
            )

        # Content scrubbing
        sanitized_content = redact_secrets_from_text(outgoing.content)

        return OutgoingMessage(
            message_id=outgoing.message_id,
            channel=outgoing.channel,
            destination=dest,
            content=sanitized_content,
            reply_to_message_id=outgoing.reply_to_message_id,
            correlation_id=outgoing.correlation_id,
            requires_approval=outgoing.requires_approval,
            approval_id=outgoing.approval_id,
            status=outgoing.status,
            error_message=outgoing.error_message,
            created_at=outgoing.created_at,
            metadata=dict(outgoing.metadata),
        )
