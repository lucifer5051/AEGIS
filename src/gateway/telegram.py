"""SAGE Remote Control Gateway -- Telegram Channel Adapter.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- Rejects non-private chat updates (groups, supergroups, channels).
- Rejects bot senders before any downstream invocation.
- Enforces message size limits.
- Zero secrets in outbound responses.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)

_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"bot\d+:[A-Za-z0-9_-]+"),
]


class TelegramAdapter:
    """Adapter translating between Telegram Bot API wire format and SAGE domain records."""

    def __init__(self, max_message_length: int = 4096) -> None:
        self._max_length = max_message_length

    def parse_update(
        self,
        raw_update: Dict[str, Any],
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Parse and validate an incoming raw Telegram update dictionary.

        Returns:
            Tuple of (is_valid, parsed_data_dict_or_none, rejection_reason_or_none).
            If is_valid is False, parsed_data_dict is None and rejection_reason explains why.
        """
        if not isinstance(raw_update, dict):
            return False, None, "Update payload must be a JSON object"

        update_id = raw_update.get("update_id")
        if update_id is None or not isinstance(update_id, int):
            return False, None, "Missing or non-integer update_id"

        # Check for message event
        message = raw_update.get("message")
        if not message:
            # Drop non-message events (channel_post, edited_message, inline_query, etc.)
            return False, None, "Unsupported or non-message update event"

        # Check chat type (must be strictly private one-to-one chat)
        chat = message.get("chat") or {}
        chat_type = chat.get("type")
        if chat_type != "private":
            return (
                False,
                None,
                f"Unauthorized chat type '{chat_type}'. Only private chats are permitted.",
            )

        chat_id = chat.get("id")
        if chat_id is None:
            return False, None, "Missing chat.id in message"

        # Check sender
        from_user = message.get("from") or {}
        if not from_user:
            return False, None, "Missing sender 'from' field in message"

        if from_user.get("is_bot") is True:
            return False, None, "Bot senders are strictly prohibited"

        user_id = from_user.get("id")
        if user_id is None or not isinstance(user_id, int):
            return False, None, "Missing or non-integer numeric sender ID"

        username = from_user.get("username")

        # Check message content and type
        text = message.get("text")
        if text is None:
            return False, None, "Only text messages are currently supported"

        if len(text) > self._max_length:
            return (
                False,
                None,
                f"Message length ({len(text)}) exceeds maximum allowed ({self._max_length})",
            )

        # Ensure no secrets contained in incoming text
        try:
            _check_no_secrets(text)
            for pat in _SECRET_VALUE_PATTERNS:
                if pat.search(text):
                    return False, None, "Rejected: payload violates zero-secret invariant"
        except Exception as exc:
            return False, None, f"Rejected: payload violates zero-secret invariant: {exc}"

        parsed_data = {
            "channel_update_id": str(update_id),
            "sender_id": str(user_id),
            "sender_username": username,
            "chat_id": str(chat_id),
            "chat_type": "private",
            "message_id": message.get("message_id"),
            "message_type": "text",
            "text": text.strip(),
            "date": message.get("date"),
        }
        return True, parsed_data, None

    def format_outbound_chunks(self, text: str, max_chunk_size: int = 4000) -> List[str]:
        """Split a long text response into Telegram-compliant message chunks.

        Avoids splitting words or lines where possible.
        """
        _check_no_secrets(text)
        if len(text) <= max_chunk_size:
            return [text]

        chunks: List[str] = []
        lines = text.splitlines(keepends=True)
        current_chunk = ""

        for line in lines:
            if len(current_chunk) + len(line) <= max_chunk_size:
                current_chunk += line
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                # If a single line exceeds chunk size, hard-split it
                while len(line) > max_chunk_size:
                    chunks.append(line[:max_chunk_size])
                    line = line[max_chunk_size:]
                current_chunk = line

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks or [text]
