"""SAGE Friday Communication Gateway -- WhatsApp Adapter.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol

Invariants:
- Reject or normalize placeholder requestId payloads (CVE-2026-48063 mitigation).
- Discard messages from self (fromMe=True).
- Strictly categorize conversation types (private vs group @g.us).
- Zero secrets in outbound messages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from src.communication.adapters.base import BaseChannelAdapter
from src.communication.domain import (
    ChannelPlatform,
    ChannelStatus,
    ConversationType,
    IncomingMessage,
    MessageDeliveryResult,
    OutboundStatus,
    OutgoingMessage,
)
from src.gateway.whatsapp_transport import WhatsAppTransport
from src.models.errors import redact_secrets_from_text

logger = logging.getLogger(__name__)

WHATSAPP_MAX_TEXT_LENGTH = 4096


def _split_text(text: str, max_chunk: int = WHATSAPP_MAX_TEXT_LENGTH) -> List[str]:
    """Split text cleanly into chunks of at most max_chunk length."""
    if len(text) <= max_chunk:
        return [text]
    chunks = []
    lines = text.split("\n")
    current_chunk = ""
    for line in lines:
        if len(current_chunk) + len(line) + 1 <= max_chunk:
            current_chunk = f"{current_chunk}\n{line}" if current_chunk else line
        else:
            if current_chunk:
                chunks.append(current_chunk)
            while len(line) > max_chunk:
                chunks.append(line[:max_chunk])
                line = line[max_chunk:]
            current_chunk = line
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


class WhatsAppChannelAdapter(BaseChannelAdapter):
    """Adapter translating between WhatsApp event streams and SAGE communication domain."""

    def __init__(
        self,
        transport: Optional[WhatsAppTransport] = None,
        channel_id: str = "whatsapp_primary",
    ) -> None:
        super().__init__(ChannelPlatform.WHATSAPP, channel_id)
        self._transport = transport
        self._connected: bool = False
        self._received_count: int = 0
        self._sent_count: int = 0
        self._error_count: int = 0
        self._seen_stanza_ids: Set[str] = set()

    def initialize(self) -> None:
        """Initialize transport connection."""
        self._connected = True

    def shutdown(self) -> None:
        """Mark adapter disconnected."""
        self._connected = False

    def health_check(self) -> ChannelStatus:
        return ChannelStatus(
            channel_id=self.channel_id,
            platform=self.platform,
            is_connected=self._connected,
            status="CONNECTED" if self._connected else "OFFLINE",
            last_seen=datetime.now(timezone.utc).isoformat(),
            messages_received=self._received_count,
            messages_sent=self._sent_count,
            error_count=self._error_count,
        )

    def _normalize_jid_to_sender(self, jid: str) -> str:
        """Extract clean sender identifier from a WhatsApp JID."""
        clean = jid.strip()
        if "@" in clean:
            return clean.split("@")[0]
        return clean

    def poll_inbound(self) -> List[IncomingMessage]:
        """Poll incoming events from transport, validate, and normalize."""
        if not self._connected or self._transport is None:
            return []

        raw_events: List[Dict[str, Any]] = []
        if hasattr(self._transport, "get_events"):
            raw_events = self._transport.get_events()
        elif hasattr(self._transport, "get_updates"):
            raw_events = self._transport.get_updates()

        messages: List[IncomingMessage] = []
        for raw in raw_events:
            # 1. Critical CVE-2026-48063 mitigation: drop placeholder requestId payloads
            if (
                "requestId" in raw
                or (isinstance(raw.get("message"), dict) and "requestId" in raw["message"])
                or (isinstance(raw.get("key"), dict) and "requestId" in raw["key"])
            ):
                continue

            # 2. Extract key components
            key = raw.get("key")
            if isinstance(key, dict):
                remote_jid = key.get("remoteJid", "")
                stanza_id = key.get("id", "")
                from_me = key.get("fromMe", False)
                participant = key.get("participant")
            else:
                remote_jid = raw.get("remoteJid") or raw.get("chat_id", "")
                stanza_id = raw.get("id") or raw.get("message_id", "")
                from_me = raw.get("fromMe", False)
                participant = raw.get("participant")

            if from_me is True or not remote_jid or not stanza_id:
                continue

            stanza_id_str = str(stanza_id)
            if stanza_id_str in self._seen_stanza_ids:
                continue
            self._seen_stanza_ids.add(stanza_id_str)

            remote_jid_str = str(remote_jid).strip()
            sender_id = self._normalize_jid_to_sender(participant or remote_jid_str)
            conv_id = remote_jid_str

            conv_type = ConversationType.PRIVATE
            if remote_jid_str.endswith("@g.us"):
                conv_type = ConversationType.GROUP
            elif "@broadcast" in remote_jid_str:
                conv_type = ConversationType.BROADCAST

            # Extract message text
            msg_payload = raw.get("message")
            text = None
            if isinstance(msg_payload, dict):
                text = (
                    msg_payload.get("conversation")
                    or (
                        msg_payload.get("extendedTextMessage", {}).get("text")
                        if isinstance(msg_payload.get("extendedTextMessage"), dict)
                        else None
                    )
                )
            elif "text" in raw:
                text = raw.get("text")

            incoming = IncomingMessage(
                message_id=stanza_id_str,
                channel=self.platform,
                sender_id=sender_id,
                conversation_id=conv_id,
                conversation_type=conv_type,
                text=text,
                is_authenticated=True,
                metadata={"remote_jid": remote_jid_str, "participant": participant},
            )
            messages.append(incoming)
            self._received_count += 1

        return messages

    def send_message(self, outgoing: OutgoingMessage) -> MessageDeliveryResult:
        """Send message through transport with auto-chunking."""
        if not self._connected:
            return MessageDeliveryResult(
                message_id=outgoing.message_id,
                channel=self.platform,
                success=False,
                status=OutboundStatus.FAILED,
                error_message="WhatsApp adapter is not connected.",
            )

        chunks = _split_text(outgoing.content)
        success = True
        err_msg = None

        dest_jid = outgoing.destination
        if not dest_jid.endswith("@s.whatsapp.net") and not dest_jid.endswith("@g.us"):
            dest_jid = f"{dest_jid}@s.whatsapp.net"

        for chunk in chunks:
            if self._transport is not None and hasattr(self._transport, "send_message"):
                try:
                    self._transport.send_message(
                        chat_id=dest_jid,
                        text=chunk,
                    )
                except Exception as exc:
                    success = False
                    err_msg = redact_secrets_from_text(str(exc))
                    self._error_count += 1
                    break
            else:
                logger.info("Mock WhatsApp message sent to %s: %s", dest_jid, chunk[:40])

        if success:
            self._sent_count += 1
            return MessageDeliveryResult(
                message_id=outgoing.message_id,
                channel=self.platform,
                success=True,
                status=OutboundStatus.SENT,
            )
        else:
            return MessageDeliveryResult(
                message_id=outgoing.message_id,
                channel=self.platform,
                success=False,
                status=OutboundStatus.FAILED,
                error_message=err_msg,
            )
