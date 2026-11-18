"""SAGE Friday Communication Gateway & Secure Sidecar Protocol.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol
"""

from src.communication.adapters.base import BaseChannelAdapter
from src.communication.adapters.telegram import TelegramChannelAdapter
from src.communication.adapters.whatsapp import WhatsAppChannelAdapter
from src.communication.domain import (
    ChannelIdentity,
    ChannelPermissionLevel,
    ChannelPlatform,
    ChannelStatus,
    CommunicationContext,
    ConversationIdentity,
    ConversationType,
    IdentityMapping,
    IncomingMessage,
    MessageAttachment,
    MessageDeliveryResult,
    OutboundStatus,
    OutgoingMessage,
)
from src.communication.errors import (
    AttachmentPolicyError,
    ChannelAuthenticationError,
    ChannelAuthorizationError,
    ChannelUnavailableError,
    CommunicationError,
    MessageValidationError,
    RateLimitExceededError,
    SidecarSecurityError,
    SidecarTimeoutError,
    UnauthorizedSenderError,
)
from src.communication.friday import FridayConversationHandler
from src.communication.gateway import CommunicationGateway
from src.communication.outbound import OutboundPolicyEngine
from src.communication.security import SecurityPolicyEnforcer
from src.communication.sidecar import (
    SecureSidecarCodec,
    SecureSidecarHost,
    SidecarEnvelope,
    SidecarEventType,
)

__all__ = [
    # Domain
    "ChannelIdentity",
    "ChannelPermissionLevel",
    "ChannelPlatform",
    "ChannelStatus",
    "CommunicationContext",
    "ConversationIdentity",
    "ConversationType",
    "IdentityMapping",
    "IncomingMessage",
    "MessageAttachment",
    "MessageDeliveryResult",
    "OutboundStatus",
    "OutgoingMessage",
    # Errors
    "CommunicationError",
    "ChannelAuthenticationError",
    "ChannelAuthorizationError",
    "UnauthorizedSenderError",
    "ChannelUnavailableError",
    "MessageValidationError",
    "SidecarSecurityError",
    "SidecarTimeoutError",
    "AttachmentPolicyError",
    "RateLimitExceededError",
    # Security & Policy
    "SecurityPolicyEnforcer",
    "OutboundPolicyEngine",
    # Sidecar
    "SidecarEventType",
    "SidecarEnvelope",
    "SecureSidecarCodec",
    "SecureSidecarHost",
    # Adapters
    "BaseChannelAdapter",
    "TelegramChannelAdapter",
    "WhatsAppChannelAdapter",
    # Friday & Orchestrator
    "FridayConversationHandler",
    "CommunicationGateway",
]
