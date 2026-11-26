"""SAGE Remote Control Gateway -- WhatsApp Channel Adapter.

Canonical Reference:
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md § 2, § 3
    AEGIS Prompt 035 (Phase 9C.2)

Invariants:
- Strictly rejects non-private conversation updates (groups `@g.us`, broadcast `@broadcast`).
- Explicitly drops `requestId` placeholder payloads (CVE-2026-48063 mitigation).
- Discards messages from self (`fromMe=True`).
- Enforces message size limits (default 4096).
- Zero-Secret Invariant: inbound and outbound payloads are strictly sanitized.
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
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
]


class WhatsAppAdapter:
    """Adapter translating between WhatsApp / Baileys event format and SAGE domain records."""

    def __init__(self, max_message_length: int = 4096) -> None:
        self._max_length = max_message_length

    def parse_update(
        self,
        raw_event: Dict[str, Any],
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Parse and validate an incoming raw WhatsApp message event dictionary.

        Returns:
            Tuple of (is_valid, parsed_data_dict_or_none, rejection_reason_or_none).
            If is_valid is False, parsed_data_dict is None and rejection_reason explains why.
        """
        if not isinstance(raw_event, dict):
            return False, None, "Update payload must be a JSON object"

        # 1. Critical CVE-2026-48063 mitigation: drop placeholder requestId payloads
        if (
            "requestId" in raw_event
            or (isinstance(raw_event.get("message"), dict) and "requestId" in raw_event["message"])
            or (isinstance(raw_event.get("key"), dict) and "requestId" in raw_event["key"])
        ):
            return (
                False,
                None,
                "Rejected: payload contains placeholder requestId (CVE-2026-48063 mitigation)",
            )

        # 2. Extract key / stanza identifiers (D9: trust normalized fields over legacy duplicates)
        if "chat_jid" in raw_event:
            # Normalized Baileys bridge payload
            remote_jid = raw_event.get("chat_jid")
            stanza_id = raw_event.get("id")
            from_me = bool(raw_event.get("from_me", False))
            is_self_chat = bool(raw_event.get("is_self_chat", False))
            participant = None
            sender_pn = raw_event.get("sender_pn")
            alt_jid = raw_event.get("alt_jid")
            text = raw_event.get("text")
            truncated = bool(raw_event.get("truncated", False))
            timestamp = raw_event.get("timestamp")
        else:
            # Legacy duplicate or flat event payload format
            key = raw_event.get("key")
            if not isinstance(key, dict):
                remote_jid = raw_event.get("remoteJid") or raw_event.get("chat_id")
                stanza_id = raw_event.get("id") or raw_event.get("message_id")
                from_me = raw_event.get("fromMe", False)
                participant = raw_event.get("participant")
            else:
                remote_jid = key.get("remoteJid")
                stanza_id = key.get("id")
                from_me = key.get("fromMe", False)
                participant = key.get("participant")
            is_self_chat = bool(raw_event.get("is_self_chat", False))
            sender_pn = None
            alt_jid = None
            text = None
            truncated = False
            timestamp = raw_event.get("messageTimestamp")

        if not remote_jid or not str(remote_jid).strip():
            return False, None, "Missing or empty remoteJid in message"

        if not stanza_id or not str(stanza_id).strip():
            return False, None, "Missing or empty message stanza ID"

        # D3: from_me Handling
        if from_me is True:
            if not is_self_chat:
                return False, None, "Ignored: message from self (fromMe=True)"

        remote_jid_str = str(remote_jid).strip()

        # 3. Conversation type check: Strictly reject groups and broadcast channels
        chat_type = raw_event.get("chat_type", "").lower()
        if (
            remote_jid_str.endswith("@g.us")
            or "@broadcast" in remote_jid_str
            or "@newsletter" in remote_jid_str
            or chat_type in ("group", "broadcast")
            or "group" in remote_jid_str.lower()
        ):
            return (
                False,
                None,
                f"Unauthorized conversation type. Group and broadcast destinations "
                f"({remote_jid_str}) are strictly prohibited.",
            )

        # 4. Sender identification
        sender_jid_str = str(participant or remote_jid_str).strip()
        if (
            sender_jid_str.endswith("@g.us")
            or "@broadcast" in sender_jid_str
            or "@newsletter" in sender_jid_str
            or "group" in sender_jid_str.lower()
        ):
            return (
                False,
                None,
                f"Unauthorized sender JID ({sender_jid_str}). "
                "Group senders are strictly prohibited.",
            )

        # 5. Extract message text (D9: switch to normalized text when available)
        if text is None:
            message = raw_event.get("message")
            if isinstance(message, dict):
                text = message.get("conversation")
                if not text and isinstance(message.get("extendedTextMessage"), dict):
                    text = message["extendedTextMessage"].get("text")
            elif isinstance(raw_event.get("text"), str):
                text = raw_event["text"]

        if text is None or not str(text).strip():
            return False, None, "Only text messages are currently supported"

        text_str = str(text).strip()
        if len(text_str) > self._max_length or truncated:
            return (
                False,
                None,
                f"Message length ({len(text_str)}) exceeds maximum allowed ({self._max_length})",
            )

        # 6. Ensure zero secrets in incoming text
        try:
            _check_no_secrets(text_str)
            for pat in _SECRET_VALUE_PATTERNS:
                if pat.search(text_str):
                    return False, None, "Rejected: payload violates zero-secret invariant"
        except Exception as exc:
            return False, None, f"Rejected: payload violates zero-secret invariant: {exc}"

        parsed_data = {
            "channel": "whatsapp",
            "channel_update_id": f"{remote_jid_str}_{stanza_id}",
            "sender_id": sender_jid_str,
            "sender_username": raw_event.get("pushName"),
            "chat_id": remote_jid_str,
            "chat_type": "private",
            "message_id": str(stanza_id),
            "message_type": "text",
            "text": text_str,
            "date": timestamp,
            "chat_jid": remote_jid_str,
            "alt_jid": alt_jid,
            "sender_pn": sender_pn,
            "from_me": from_me,
            "is_self_chat": is_self_chat,
            "is_owner": bool(from_me and is_self_chat),
        }
        return True, parsed_data, None

    def format_outbound_chunks(self, text: str, max_chunk_size: int = 4096) -> List[str]:
        """Split a long text response into WhatsApp-compliant message chunks."""
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
                    chunks.append(current_chunk.rstrip("\r\n"))
                current_chunk = line

        if current_chunk:
            chunks.append(current_chunk.rstrip("\r\n"))

        return chunks
