"""SAGE Remote Control Gateway -- Domain Models.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- All models are frozen and immutable (extra="forbid").
- Zero-Secret Invariant: Update text, metadata, and responses must never contain raw credentials.
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.trust.audit import _check_no_secrets


@unique
class UpdateLifecycleState(str, Enum):
    """Lifecycle state of an inbound channel update."""

    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@unique
class OutboundStatus(str, Enum):
    """Delivery status of an outbound response or notification message."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@unique
class ChannelPlatform(str, Enum):
    """Supported external messaging platforms."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DESKTOP = "desktop"
    SYSTEM_CLI = "system_cli"


@unique
class ConversationType(str, Enum):
    """Category of conversation destination."""

    PRIVATE = "private"
    GROUP = "group"
    BROADCAST = "broadcast"


class NormalizedInboundMessage(BaseModel):
    """Channel-neutral normalized representation of an incoming message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: ChannelPlatform = Field(description="Originating channel platform.")
    message_id: str = Field(description="Stable provider message / stanza identifier.")
    conversation_id: str = Field(description="Conversation / chat identifier.")
    sender_id: str = Field(description="Sender account or user identifier.")
    recipient_id: Optional[str] = Field(
        default=None, description="Direct recipient / bot identifier if present."
    )
    conversation_type: ConversationType = Field(
        default=ConversationType.PRIVATE, description="Conversation category."
    )
    text: Optional[str] = Field(default=None, description="Extracted text content.")
    received_at: str = Field(description="ISO-8601 UTC timestamp of receipt.")
    correlation_id: Optional[str] = Field(
        default=None, description="Correlation identifier for tracing."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Structured provider-specific metadata."
    )

    @field_validator("text")
    @classmethod
    def validate_no_secrets_in_text(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_no_secrets(v)
        return v


class NormalizedOutboundMessage(BaseModel):
    """Channel-neutral normalized representation of an outbound message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(description="Unique message identifier (UUID).")
    channel: ChannelPlatform = Field(description="Target channel platform.")
    destination: str = Field(description="Target recipient or chat identifier.")
    content: str = Field(description="Exact message text content.")
    content_hash: str = Field(description="SHA-256 hex digest of exact content.")
    correlation_id: Optional[str] = Field(
        default=None, description="Correlation / request identifier."
    )
    requires_approval: bool = Field(
        default=False, description="Whether delivery requires explicit Trust Core approval."
    )
    approval_id: Optional[str] = Field(
        default=None, description="Linked Trust Core approval request ID if approved."
    )
    status: OutboundStatus = Field(default=OutboundStatus.PENDING, description="Delivery status.")
    error_message: Optional[str] = Field(
        default=None, description="Failure or uncertainty error details."
    )
    created_at: str = Field(description="ISO-8601 UTC creation timestamp.")
    updated_at: str = Field(description="ISO-8601 UTC last-updated timestamp.")

    @field_validator("content")
    @classmethod
    def validate_no_secrets_in_content(cls, v: str) -> str:
        _check_no_secrets(v)
        return v


class InboundUpdate(BaseModel):
    """Immutable representation of an inbound channel update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    update_id: str = Field(description="Unique internal update identifier (UUID).")
    channel: str = Field(default="telegram", description="Originating messaging channel.")
    channel_update_id: str = Field(description="Provider-specific update ID for deduplication.")
    sender_id: str = Field(description="Authenticated numeric sender ID (as string).")
    chat_id: str = Field(description="Messaging chat ID.")
    chat_type: str = Field(default="private", description="Chat category ('private').")
    message_type: str = Field(default="text", description="Message content type.")
    raw_text: Optional[str] = Field(default=None, description="Message text content.")
    state: UpdateLifecycleState = Field(
        default=UpdateLifecycleState.RECEIVED, description="Current processing state."
    )
    task_id: Optional[str] = Field(default=None, description="Associated SAGE task ID if created.")
    retry_count: int = Field(default=0, ge=0, description="Processing retry count.")
    max_retries: int = Field(default=3, ge=1, description="Maximum allowed retries.")
    error_summary: Optional[str] = Field(default=None, description="Redacted failure summary.")
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Structured update metadata."
    )
    created_at: str = Field(description="ISO-8601 UTC creation timestamp.")
    updated_at: str = Field(description="ISO-8601 UTC last-updated timestamp.")

    @field_validator("raw_text")
    @classmethod
    def validate_no_secrets_in_text(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_no_secrets(v)
        return v

    @field_validator("error_summary")
    @classmethod
    def validate_no_secrets_in_error(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_no_secrets(v)
        return v


class OutboundMessage(BaseModel):
    """Immutable representation of a message queued or sent to an external channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(description="Unique message identifier (UUID).")
    channel: str = Field(default="telegram", description="Target messaging channel.")
    recipient_id: str = Field(description="Target recipient chat or user ID.")
    content: str = Field(description="Sanitized outbound text content.")
    inbound_update_id: Optional[str] = Field(
        default=None, description="Inbound update ID that triggered this message."
    )
    task_id: Optional[str] = Field(default=None, description="Associated SAGE task ID if any.")
    status: OutboundStatus = Field(
        default=OutboundStatus.PENDING, description="Current delivery status."
    )
    attempts: int = Field(default=0, ge=0, description="Delivery attempt counter.")
    max_attempts: int = Field(default=3, ge=1, description="Maximum delivery attempts.")
    last_attempt_at: Optional[str] = Field(default=None, description="Timestamp of last attempt.")
    error_message: Optional[str] = Field(default=None, description="Delivery error details.")
    content_hash: Optional[str] = Field(
        default=None, description="SHA-256 hash of outbound content."
    )
    requires_approval: bool = Field(
        default=False, description="Whether delivery requires explicit Trust Core approval."
    )
    approval_id: Optional[str] = Field(
        default=None, description="Linked Trust Core approval request ID if approved."
    )
    created_at: str = Field(description="ISO-8601 UTC creation timestamp.")
    updated_at: str = Field(description="ISO-8601 UTC update timestamp.")

    @field_validator("content")
    @classmethod
    def validate_no_secrets_in_content(cls, v: str) -> str:
        _check_no_secrets(v)
        return v


class RouterResponse(BaseModel):
    """Result returned by the CommandRouter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    response_text: str = Field(description="Sanitized response message to return to user.")
    task_id: Optional[str] = Field(default=None, description="Task ID if created or operated on.")
    awaiting_approval: bool = Field(
        default=False, description="True if task or briefing requires approval."
    )
    completed: bool = Field(
        default=True, description="True if command completed without pending user action."
    )


class ProcessResult(BaseModel):
    """Result summary of processing an individual channel update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    update_id: str = Field(description="Internal update identifier.")
    channel_update_id: str = Field(description="Channel-specific update ID.")
    sender_id: str = Field(description="Sender ID.")
    state: UpdateLifecycleState = Field(description="Final lifecycle state.")
    response_text: Optional[str] = Field(default=None, description="Response dispatched to user.")
    task_id: Optional[str] = Field(default=None, description="Associated SAGE task ID.")
    error_summary: Optional[str] = Field(
        default=None, description="Failure summary if rejected or failed."
    )
