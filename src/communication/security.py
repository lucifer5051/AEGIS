"""SAGE Friday Communication Gateway -- Security & Authorization.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol

Invariants:
- Fail-closed: Unknown senders and unmapped accounts have zero permissions.
- TrustCore authority: Possession of an account never equates to blanket authorization.
- Group isolation: Group chats never inherit admin privileges.
- Attachment safety: Strictly validated file basenames, types, and byte limits.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.communication.domain import (
    ChannelPermissionLevel,
    ChannelPlatform,
    CommunicationContext,
    ConversationType,
    IdentityMapping,
    IncomingMessage,
    MessageAttachment,
)
from src.communication.errors import (
    AttachmentPolicyError,
    ChannelAuthorizationError,
    UnauthorizedSenderError,
)

logger = logging.getLogger(__name__)

# Permitted attachment extensions and MIME types
ALLOWED_ATTACHMENT_EXTENSIONS: Set[str] = {
    ".txt",
    ".md",
    ".json",
    ".csv",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}
MAX_ATTACHMENT_BYTES: int = 10 * 1024 * 1024  # 10 MB limit


class SecurityPolicyEnforcer:
    """Enforces sender authentication, identity resolution, permission checks, and attachment validation."""

    def __init__(
        self,
        allowed_senders: Optional[Dict[Tuple[ChannelPlatform, str], IdentityMapping]] = None,
        allowed_groups: Optional[Set[Tuple[ChannelPlatform, str]]] = None,
        admin_users: Optional[Set[str]] = None,
        default_attachment_dir: Optional[Path] = None,
    ) -> None:
        self._identities: Dict[Tuple[ChannelPlatform, str], IdentityMapping] = (
            dict(allowed_senders) if allowed_senders else {}
        )
        self._allowed_groups: Set[Tuple[ChannelPlatform, str]] = (
            set(allowed_groups) if allowed_groups else set()
        )
        self._admin_users: Set[str] = set(admin_users) if admin_users else set()
        self._attachment_dir = default_attachment_dir or (Path.cwd() / "attachments")

    def register_identity(self, mapping: IdentityMapping) -> None:
        """Register or update an authorized identity mapping."""
        key = (mapping.channel, str(mapping.external_sender_id).strip())
        self._identities[key] = mapping

    def allow_group(self, channel: ChannelPlatform, group_id: str) -> None:
        """Allow a group conversation ID for participation."""
        self._allowed_groups.add((channel, str(group_id).strip()))

    def resolve_context(self, message: IncomingMessage) -> CommunicationContext:
        """Resolve and authenticate the communication context from an incoming message.

        Raises:
            UnauthorizedSenderError: If sender is unmapped, inactive, or conversation is rejected.
        """
        sender_id = str(message.sender_id).strip()
        conv_id = str(message.conversation_id).strip()

        # Reject empty or whitespace identifiers
        if not sender_id:
            raise UnauthorizedSenderError("Incoming message contains empty sender_id.")
        if not conv_id:
            raise UnauthorizedSenderError("Incoming message contains empty conversation_id.")

        # Group conversation policy
        if message.conversation_type == ConversationType.GROUP:
            if (message.channel, conv_id) not in self._allowed_groups:
                raise UnauthorizedSenderError(
                    f"Group conversation '{conv_id}' on channel '{message.channel.value}' is not authorized."
                )

        # Lookup identity
        key = (message.channel, sender_id)
        identity = self._identities.get(key)
        if not identity or not identity.is_active:
            raise UnauthorizedSenderError(
                f"Sender '{sender_id}' on channel '{message.channel.value}' is unauthorized or inactive."
            )

        permissions = list(identity.permissions)

        # Enforce group isolation invariant:
        # Group conversations can NEVER hold ADMIN or APPROVE permissions, even if user is admin in private.
        if message.conversation_type == ConversationType.GROUP:
            permissions = [
                p
                for p in permissions
                if p not in (ChannelPermissionLevel.ADMIN, ChannelPermissionLevel.APPROVE)
            ]

        # Admin user override in private chat
        if message.conversation_type == ConversationType.PRIVATE and identity.sage_user_id in self._admin_users:
            if ChannelPermissionLevel.ADMIN not in permissions:
                permissions.append(ChannelPermissionLevel.ADMIN)

        session_id = f"{message.channel.value}:{conv_id}:{identity.sage_user_id}"

        return CommunicationContext(
            channel=message.channel,
            sender_id=sender_id,
            conversation_id=conv_id,
            conversation_type=message.conversation_type,
            user_id=identity.sage_user_id,
            display_name=identity.display_name,
            permissions=permissions,
            session_id=session_id,
            metadata=dict(message.metadata),
        )

    def check_permission(
        self, context: CommunicationContext, required_level: ChannelPermissionLevel
    ) -> None:
        """Assert context possesses the required permission level.

        Raises:
            ChannelAuthorizationError: If permission is lacking.
        """
        if not context.has_permission(required_level):
            raise ChannelAuthorizationError(
                f"User '{context.user_id or context.sender_id}' lacks required permission "
                f"'{required_level.value}' on {context.channel.value}."
            )

    def validate_attachment(self, attachment: MessageAttachment, raw_bytes: Optional[bytes] = None) -> MessageAttachment:
        """Validate and verify safety of an inbound attachment.

        Raises:
            AttachmentPolicyError: On path traversal, oversized file, or forbidden extension.
        """
        # Clean basename and detect traversal
        safe_name = os.path.basename(attachment.filename)
        if ".." in attachment.filename or safe_name != attachment.filename:
            raise AttachmentPolicyError(
                f"Dangerous attachment filename detected: '{attachment.filename}'"
            )

        ext = os.path.splitext(safe_name)[1].lower()
        if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
            raise AttachmentPolicyError(
                f"Attachment extension '{ext}' is prohibited. Allowed: {sorted(ALLOWED_ATTACHMENT_EXTENSIONS)}"
            )

        if attachment.size_bytes > MAX_ATTACHMENT_BYTES:
            raise AttachmentPolicyError(
                f"Attachment size ({attachment.size_bytes} bytes) exceeds {MAX_ATTACHMENT_BYTES} byte limit."
            )

        sha = attachment.sha256
        if raw_bytes is not None:
            if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
                raise AttachmentPolicyError("Raw attachment payload exceeds maximum byte limit.")
            calculated = hashlib.sha256(raw_bytes).hexdigest()
            if sha and sha.lower() != calculated.lower():
                raise AttachmentPolicyError("Attachment SHA-256 digest mismatch.")
            sha = calculated

        return MessageAttachment(
            attachment_id=attachment.attachment_id,
            filename=safe_name,
            content_type=attachment.content_type,
            size_bytes=attachment.size_bytes,
            file_path=attachment.file_path,
            sha256=sha,
            is_validated=True,
        )
