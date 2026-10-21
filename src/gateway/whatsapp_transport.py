"""SAGE Remote Control Gateway -- Offline Simulated WhatsApp Transport.

Canonical Reference:
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md § 2, § 3
    AEGIS Prompt 035 (Phase 9C.2)

Invariants:
- 100% offline; zero network calls; zero external credentials required.
- Simulates deterministic Baileys message events, disconnects, reconnects, and delivery outcomes.
- Independent outbound group / broadcast rejection before send attempt.
- Exact-content and exact-recipient approval enforcement for AI-generated outbound messages.
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from src.gateway.errors import TransportError
from src.gateway.models import OutboundMessage, OutboundStatus
from src.trust.approval import ApprovalStore
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


class WhatsAppTransport(ABC):
    """Abstract transport interface for WhatsApp / Baileys communication."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the transport socket is actively connected."""

    @abstractmethod
    def get_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch incoming normalized or raw events from the transport queue."""

    @abstractmethod
    def send_message(
        self,
        recipient_jid: Optional[Union[str, OutboundMessage]] = None,
        text: Optional[str] = None,
        correlation_id: Optional[str] = None,
        approval_id: Optional[str] = None,
        approval_store: Optional[ApprovalStore] = None,
        require_approval: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Send a message to a remote recipient JID."""


class SimulatedWhatsAppTransport(WhatsAppTransport):
    """Deterministic, in-memory fake transport for offline testing of WhatsApp workflows."""

    def __init__(
        self,
        store: Optional[ApprovalStore] = None,
        approval_store: Optional[ApprovalStore] = None,
    ) -> None:
        self._connected: bool = True
        self._inbound_queue: List[Dict[str, Any]] = []
        self.sent_messages: List[Dict[str, Any]] = []
        self._fail_next_send: Optional[Exception] = None
        self._uncertain_next_send: bool = False
        self._next_stanza_counter: int = 1000
        self._approval_store: Optional[ApprovalStore] = store or approval_store

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """Simulate socket connection / reconnection."""
        self._connected = True

    def disconnect(self) -> None:
        """Simulate socket disconnection."""
        self._connected = False

    def set_fail_next_send(self, exc: Exception) -> None:
        """Configure transport to raise an exception on next send_message."""
        self._fail_next_send = exc

    def set_uncertain_next_send(self, uncertain: bool = True) -> None:
        """Configure transport to simulate a TCP drop before server ACK (delivery uncertain)."""
        self._uncertain_next_send = uncertain

    def set_uncertain_delivery(self, uncertain: bool = True) -> None:
        """Alias for set_uncertain_next_send."""
        self.set_uncertain_next_send(uncertain)

    def enqueue_raw_event(self, event: Dict[str, Any]) -> None:
        """Enqueue a raw dictionary event."""
        self._inbound_queue.append(event)

    def enqueue_synthetic_message(
        self,
        remote_jid: Optional[str] = None,
        text: str = "",
        stanza_id: Optional[str] = None,
        sender_jid: Optional[str] = None,
        message_id: Optional[str] = None,
        conversation_type: Optional[str] = None,
        is_group: bool = False,
        is_broadcast: bool = False,
        has_request_id: bool = False,
        push_name: str = "Authorized Contact",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Construct and enqueue a synthetic Baileys message event."""
        if stanza_id is None:
            if message_id is not None:
                stanza_id = message_id
            else:
                stanza_id = f"WA_{self._next_stanza_counter}"
                self._next_stanza_counter += 1

        target_jid = remote_jid or sender_jid or ""
        if conversation_type == "group":
            is_group = True
        elif conversation_type == "broadcast":
            is_broadcast = True

        if is_group and not target_jid.endswith("@g.us"):
            target_jid = f"{target_jid}@g.us"
        elif is_broadcast and not target_jid.endswith("@broadcast"):
            target_jid = f"{target_jid}@broadcast"

        actual_sender = sender_jid or target_jid

        event: Dict[str, Any] = {
            "key": {
                "remoteJid": target_jid,
                "fromMe": False,
                "id": stanza_id,
                "participant": actual_sender if is_group else None,
            },
            "message": {
                "conversation": text,
            },
            "messageTimestamp": int(time.time()),
            "pushName": push_name,
            "chat_type": "group" if is_group else ("broadcast" if is_broadcast else "private"),
        }

        if has_request_id:
            event["requestId"] = "req_placeholder_test"

        self._inbound_queue.append(event)
        return event

    def get_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve queued events in FIFO order. Disconnected transport raises TransportError."""
        if not self._connected:
            raise TransportError("WhatsApp transport is disconnected.")

        to_return = self._inbound_queue[:limit]
        self._inbound_queue = self._inbound_queue[limit:]
        return to_return

    def send_message(
        self,
        recipient_jid: Optional[Union[str, OutboundMessage]] = None,
        text: Optional[str] = None,
        correlation_id: Optional[str] = None,
        approval_id: Optional[str] = None,
        approval_store: Optional[ApprovalStore] = None,
        require_approval: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Simulate message dispatch with security validations and simulated failure injection."""
        is_outbound_model = False
        outbound_model: Optional[OutboundMessage] = None

        if (
            isinstance(recipient_jid, OutboundMessage)
            or hasattr(recipient_jid, "recipient_id")
            or hasattr(recipient_jid, "destination")
            or hasattr(recipient_jid, "chat_id")
        ):
            is_outbound_model = True
            outbound_model = recipient_jid  # type: ignore
            target_jid = (
                getattr(outbound_model, "recipient_id", None)
                or getattr(outbound_model, "destination", None)
                or getattr(outbound_model, "chat_id", None)
                or ""
            )
            content_text = (
                getattr(outbound_model, "content", None)
                or getattr(outbound_model, "text", None)
                or text
                or ""
            )
            correlation_id = (
                correlation_id
                or getattr(outbound_model, "inbound_update_id", None)
                or getattr(outbound_model, "correlation_id", None)
            )
            approval_id = approval_id or getattr(outbound_model, "approval_id", None)
            require_approval = require_approval or getattr(
                outbound_model, "requires_approval", False
            )
        else:
            target_jid = recipient_jid or ""
            content_text = text or ""

        effective_approval_store = approval_store or self._approval_store

        # 1. Connection check
        if not self._connected:
            raise TransportError("WhatsApp transport is disconnected.")

        # 2. Zero-secret check
        _check_no_secrets(content_text)

        # 3. Independent outbound destination validation
        if (
            target_jid.endswith("@g.us")
            or target_jid.endswith("@broadcast")
            or "group" in target_jid.lower()
        ):
            raise TransportError(
                f"Outbound dispatch rejected: destination '{target_jid}' is a group "
                "or broadcast channel. Group destinations are strictly rejected."
            )

        # 4. Outbound approval verification
        content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()

        if require_approval:
            if not approval_id:
                raise TransportError("Outbound transmission requires explicit human approval ID.")
            if effective_approval_store is None:
                raise TransportError("ApprovalStore not provided to verify outbound approval.")

            rec = effective_approval_store.get_request(approval_id)
            if not rec:
                raise TransportError(f"Approval request '{approval_id}' not found.")
            if rec.status != "APPROVED":
                raise TransportError(
                    f"Approval request '{approval_id}' is in non-approved status '{rec.status}'."
                )
            if rec.is_expired():
                raise TransportError(f"Approval request '{approval_id}' is expired.")

            # Verify exact-recipient binding
            if rec.target_resource != target_jid and rec.target_resource != "*":
                raise TransportError(
                    f"Approval target mismatch: approved for '{rec.target_resource}', "
                    f"attempted send to '{target_jid}'."
                )

            # Verify exact-content binding
            if content_hash not in rec.parameters_summary:
                raise TransportError(
                    "Approval content hash mismatch: "
                    "message content was modified after approval was granted."
                )

        # 5. Simulated failure injection
        if self._fail_next_send is not None:
            exc = self._fail_next_send
            self._fail_next_send = None
            raise exc

        # 6. Simulated uncertain delivery (TCP disconnect before server ACK)
        if self._uncertain_next_send:
            self._uncertain_next_send = False
            self.disconnect()
            if is_outbound_model and outbound_model is not None:
                outbound_model.status = OutboundStatus.UNCERTAIN
                outbound_model.error_message = (
                    "Simulated TCP socket drop before server ACK: uncertain delivery."
                )
                return outbound_model
            raise TransportError("Simulated TCP socket drop before server ACK: uncertain delivery.")

        # 7. Successful delivery recording
        msg_id = str(uuid4())
        record = {
            "message_id": msg_id,
            "recipient_jid": target_jid,
            "text": content_text,
            "content_hash": content_hash,
            "correlation_id": correlation_id,
            "approval_id": approval_id,
            "status": "SENT",
            "timestamp": int(time.time()),
        }
        self.sent_messages.append(record)

        if is_outbound_model and outbound_model is not None:
            outbound_model.status = OutboundStatus.SENT
            outbound_model.message_id = msg_id
            return outbound_model

        return {"status": "SENT", "message_id": msg_id, "timestamp": record["timestamp"]}
