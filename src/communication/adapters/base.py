"""SAGE Friday Communication Gateway -- Channel Adapter Interface.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from src.communication.domain import (
    ChannelIdentity,
    ChannelPlatform,
    ChannelStatus,
    IncomingMessage,
    MessageDeliveryResult,
    OutgoingMessage,
)


class BaseChannelAdapter(ABC):
    """Abstract base class for messaging platform adapters."""

    def __init__(self, platform: ChannelPlatform, channel_id: str) -> None:
        self.platform = platform
        self.channel_id = channel_id

    @abstractmethod
    def initialize(self) -> None:
        """Start or connect the channel adapter."""

    @abstractmethod
    def shutdown(self) -> None:
        """Gracefully disconnect and release resources."""

    @abstractmethod
    def send_message(self, outgoing: OutgoingMessage) -> MessageDeliveryResult:
        """Transmit a normalized outbound message to the target platform."""

    @abstractmethod
    def poll_inbound(self) -> List[IncomingMessage]:
        """Fetch pending incoming messages normalized to the standard representation."""

    @abstractmethod
    def health_check(self) -> ChannelStatus:
        """Return connectivity state and operational health metrics."""
