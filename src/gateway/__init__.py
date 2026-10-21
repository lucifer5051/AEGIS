"""SAGE Remote Control Gateway Subsystem (Telegram-First).

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)
"""

from __future__ import annotations

from src.gateway.errors import (
    AuthenticationError,
    ChannelConfigError,
    CommandExecutionError,
    DuplicateUpdateError,
    GatewayError,
    RateLimitExceededError,
    TransportError,
    UpdateValidationError,
)
from src.gateway.gateway import ChannelGateway
from src.gateway.models import (
    ChannelPlatform,
    ConversationType,
    InboundUpdate,
    NormalizedInboundMessage,
    NormalizedOutboundMessage,
    OutboundMessage,
    OutboundStatus,
    ProcessResult,
    RouterResponse,
    UpdateLifecycleState,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import CommandRouter
from src.gateway.sidecar_protocol import (
    SidecarEnvelope,
    SidecarEventType,
    parse_sidecar_line,
    serialize_sidecar_envelope,
)
from src.gateway.store import GatewayStore
from src.gateway.telegram import TelegramAdapter
from src.gateway.transport import (
    HttpTelegramTransport,
    MockTelegramTransport,
    TelegramTransport,
)
from src.gateway.whatsapp import WhatsAppAdapter
from src.gateway.whatsapp_transport import (
    SimulatedWhatsAppTransport,
    WhatsAppTransport,
)

__all__ = [
    "AuthenticationError",
    "ChannelConfigError",
    "ChannelGateway",
    "ChannelPlatform",
    "CommandExecutionError",
    "CommandRouter",
    "ConversationType",
    "DuplicateUpdateError",
    "GatewayError",
    "GatewayStore",
    "HttpTelegramTransport",
    "InboundUpdate",
    "MockTelegramTransport",
    "NormalizedInboundMessage",
    "NormalizedOutboundMessage",
    "OutboundMessage",
    "OutboundStatus",
    "ProcessResult",
    "RateLimitExceededError",
    "RouterResponse",
    "SidecarEnvelope",
    "SidecarEventType",
    "SimulatedWhatsAppTransport",
    "SlidingWindowRateLimiter",
    "TelegramAdapter",
    "TelegramTransport",
    "TransportError",
    "UpdateLifecycleState",
    "UpdateValidationError",
    "WhatsAppAdapter",
    "WhatsAppTransport",
    "parse_sidecar_line",
    "serialize_sidecar_envelope",
]
