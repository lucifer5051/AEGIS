"""SAGE Friday Communication Gateway -- Domain Models.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol

Invariants:
- All models are frozen and immutable (extra="forbid").
- Zero-Secret Invariant: Message text, metadata, and responses must never contain raw secrets.
- Deterministic UTC timestamps throughout.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.models.errors import redact_secrets_from_text
from src.trust.audit import _check_no_secrets

_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"bot\d+:[A-Za-z0-9_-]+"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@unique
class ChannelPlatform(str, Enum):
    """Supported messaging channel platforms."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DESKTOP = "desktop"
    SYSTEM_CLI = "system_cli"
    MOCK = "mock"


@unique
class ConversationType(str, Enum):
    """Conversation visibility and participant boundary."""

    PRIVATE = "private"
    GROUP = "group"
    BROADCAST = "broadcast"


@unique
class OutboundStatus(str, Enum):
    """Delivery status of an outbound response or notification."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@unique
class ChannelPermissionLevel(str, Enum):
    """Explicit permission tiers for communication actors."""

    RECEIVE = "RECEIVE"
    CHAT = "CHAT"
    MEMORY_ACCESS = "MEMORY_ACCESS"
    TASK_REQUEST = "TASK_REQUEST"
    APPROVE = "APPROVE"
    ADMIN = "ADMIN"


class MessageAttachment(BaseModel):
    """Metadata representing an attachment transmitted over a channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attachment_id: str = Field(default_factory=lambda: str(uuid4()))
    filename: str = Field(description="Sanitized basename of the file.")
    content_type: str = Field(description="MIME type classification.")
    size_bytes: int = Field(ge=0, description="Payload byte length.")
    file_path: Optional[str] = Field(default=None, description="Controlled application path.")
    sha256: Optional[str] = Field(default=None, description="SHA-256 payload digest.")
    is_validated: bool = Field(default=False, description="Whether safety validation passed.")


class IncomingMessage(BaseModel):
    """Channel-neutral normalized representation of an incoming message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(description="Unique message identifier from platform.")
    channel: ChannelPlatform = Field(description="Originating channel platform.")
    sender_id: str = Field(description="Authenticated sender identity on channel.")
    conversation_id: str = Field(description="Conversation or chat room identifier.")
    conversation_type: ConversationType = Field(
        default=ConversationType.PRIVATE, description="Private or group conversation."
    )
    text: Optional[str] = Field(default=None, description="Extracted message text content.")
    received_at: str = Field(default_factory=_utc_now_iso, description="ISO-8601 UTC timestamp.")
    reply_to_message_id: Optional[str] = Field(
        default=None, description="Parent message ID if this is a reply."
    )
    attachments: List[MessageAttachment] = Field(
        default_factory=list, description="Associated message attachments."
    )
    is_authenticated: bool = Field(
        default=False, description="Whether the message passed transport verification."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Platform-specific metadata."
    )

    @field_validator("text")
    @classmethod
    def validate_no_secrets_in_text(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_no_secrets(v)
            for pat in _SECRET_VALUE_PATTERNS:
                if pat.search(v):
                    raise ValueError("Incoming message text violates zero-secret invariant.")
        return v


class OutgoingMessage(BaseModel):
    """Channel-neutral normalized representation of an outbound response or alert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(default_factory=lambda: str(uuid4()))
    channel: ChannelPlatform = Field(description="Destination channel platform.")
    destination: str = Field(description="Target recipient, user, or conversation ID.")
    content: str = Field(description="Sanitized response text content.")
    content_hash: str = Field(default="", description="SHA-256 digest of content.")
    reply_to_message_id: Optional[str] = Field(
        default=None, description="Message to reply to if supported."
    )
    correlation_id: Optional[str] = Field(
        default=None, description="Trace or request correlation ID."
    )
    requires_approval: bool = Field(
        default=False, description="Whether delivery requires explicit TrustCore approval."
    )
    approval_id: Optional[str] = Field(
        default=None, description="Linked TrustCore approval request ID."
    )
    status: OutboundStatus = Field(
        default=OutboundStatus.PENDING, description="Current dispatch status."
    )
    error_message: Optional[str] = Field(
        default=None, description="Delivery failure explanation if failed."
    )
    created_at: str = Field(default_factory=_utc_now_iso, description="ISO-8601 UTC creation.")
    updated_at: str = Field(default_factory=_utc_now_iso, description="ISO-8601 UTC update.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Outbound metadata.")

    def model_post_init(self, __context: Any) -> None:
        if not self.content_hash and self.content:
            chash = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
            object.__setattr__(self, "content_hash", chash)

    @field_validator("content")
    @classmethod
    def validate_no_secrets_in_content(cls, v: str) -> str:
        _check_no_secrets(v)
        for pat in _SECRET_VALUE_PATTERNS:
            if pat.search(v):
                raise ValueError("Outgoing message content violates zero-secret invariant.")
        return v


class ConversationIdentity(BaseModel):
    """Identity tracking for an active conversation thread."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str = Field(description="Channel-specific conversation ID.")
    channel: ChannelPlatform = Field(description="Platform of the conversation.")
    conversation_type: ConversationType = Field(default=ConversationType.PRIVATE)
    title: Optional[str] = Field(default=None, description="Group title or user handle.")
    is_authorized: bool = Field(default=False, description="Whether conversation is allowed.")


class ChannelIdentity(BaseModel):
    """Metadata representing an active messaging channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_id: str = Field(description="Unique internal channel identifier.")
    platform: ChannelPlatform = Field(description="Platform implementation.")
    display_name: str = Field(description="User-facing channel label.")
    is_connected: bool = Field(default=False, description="Whether channel is online.")
    status: str = Field(default="OFFLINE", description="Current channel status.")


class MessageDeliveryResult(BaseModel):
    """Outcome of attempting to transmit an outbound message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    channel: ChannelPlatform
    success: bool
    status: OutboundStatus
    error_message: Optional[str] = None
    timestamp: str = Field(default_factory=_utc_now_iso)


class ChannelStatus(BaseModel):
    """Operational metrics and connectivity state for a communication channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_id: str
    platform: ChannelPlatform
    is_connected: bool
    status: str
    last_seen: Optional[str] = None
    messages_received: int = 0
    messages_sent: int = 0
    error_count: int = 0


class IdentityMapping(BaseModel):
    """Deterministic mapping from external channel identity to internal SAGE user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_sender_id: str = Field(description="Sender ID on the external channel.")
    channel: ChannelPlatform = Field(description="Originating channel.")
    sage_user_id: str = Field(description="Internal SAGE user/owner ID.")
    display_name: str = Field(default="Authorized User")
    permissions: List[ChannelPermissionLevel] = Field(
        default_factory=lambda: [ChannelPermissionLevel.RECEIVE, ChannelPermissionLevel.CHAT]
    )
    is_active: bool = Field(default=True)


class CommunicationContext(BaseModel):
    """Security and execution context for an authenticated communication interaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: ChannelPlatform
    sender_id: str
    conversation_id: str
    conversation_type: ConversationType = ConversationType.PRIVATE
    user_id: Optional[str] = None
    display_name: Optional[str] = None
    permissions: List[ChannelPermissionLevel] = Field(default_factory=list)
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def has_permission(self, level: ChannelPermissionLevel) -> bool:
        """Check if context holds the requested permission or ADMIN."""
        if ChannelPermissionLevel.ADMIN in self.permissions:
            return True
        return level in self.permissions
