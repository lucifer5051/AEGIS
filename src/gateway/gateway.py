"""SAGE Remote Control Gateway -- Central Orchestrator.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- The connector is disabled by default.
- Disabled connector NEVER polls or accesses network.
- Telegram authentication establishes request source, NOT blanket authorization.
- Unauthorized senders and malformed updates are rejected before inference or task creation.
- Tasks and updates use separate state machines.
- All operations are 100% testable offline with zero real credentials.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

from src.config.models import TelegramConfig, WhatsAppConfig
from src.gateway.contact_alias import CANONICAL_PN_REGEX, ContactAliasStore
from src.gateway.echo_registry import SelfEchoRegistry
from src.gateway.errors import ChannelConfigError, TransportError
from src.gateway.local_draft_provider import is_urgent_message
from src.gateway.models import (
    OutboundStatus,
    ProcessResult,
    UpdateLifecycleState,
)
from src.gateway.owner_channel import DefaultOwnerChannelHandler, OwnerChannelHandler
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import CommandRouter
from src.gateway.store import GatewayStore
from src.gateway.telegram import TelegramAdapter
from src.gateway.transport import TelegramTransport
from src.gateway.whatsapp import WhatsAppAdapter
from src.gateway.whatsapp_transport import SimulatedWhatsAppTransport, WhatsAppTransport
from src.manager.models import TaskState
from src.memory.contact_memory import EncryptedContactMemoryStore
from src.trust.approval import ApprovalStore
from src.trust.audit import AuditDecision, AuditEventType, AuditWriter

logger = logging.getLogger(__name__)


def mask_phone_number(jid_or_phone: str) -> str:
    """Mask phone number in JID or string to last 2 digits for privacy."""
    if not jid_or_phone or not isinstance(jid_or_phone, str):
        return "unknown"
    parts = jid_or_phone.split("@")
    pn = parts[0]
    suffix = f"@{parts[1]}" if len(parts) > 1 else ""
    if len(pn) <= 2:
        masked_pn = "**"
    else:
        masked_pn = "*" * (len(pn) - 2) + pn[-2:]
    return f"{masked_pn}{suffix}"


class ChannelGateway:
    """Central gateway coordinator managing remote inbound updates and outbound responses."""

    def __init__(
        self,
        config: Union[TelegramConfig, WhatsAppConfig],
        store: GatewayStore,
        transport: Union[TelegramTransport, WhatsAppTransport],
        router: CommandRouter,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        adapter: Optional[Union[TelegramAdapter, WhatsAppAdapter]] = None,
        approval_store: Optional[ApprovalStore] = None,
        contact_memory_store: Optional[EncryptedContactMemoryStore] = None,
        alias_store: Optional[ContactAliasStore] = None,
        echo_registry: Optional[SelfEchoRegistry] = None,
        owner_channel_handler: Optional[OwnerChannelHandler] = None,
        audit_writer: Optional[AuditWriter] = None,
        draft_manager: Optional[Any] = None,
        supervisor: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._store = store
        self._transport = transport
        self._router = router
        self._limiter = rate_limiter or SlidingWindowRateLimiter()
        self._approval_store = approval_store or getattr(router, "_approval_store", None)
        self._contact_memory_store = contact_memory_store
        if draft_manager is not None:
            self._draft_manager = draft_manager
        elif isinstance(config, WhatsAppConfig) and contact_memory_store is not None and hasattr(contact_memory_store, "_key_store") and hasattr(store, "_conn"):
            from src.gateway.drafts import DraftManager
            self._draft_manager = DraftManager(
                conn=store._conn,
                key_store=contact_memory_store._key_store,
                audit_writer=audit_writer,
            )
        else:
            self._draft_manager = None
        self._supervisor = supervisor
        self._channel = "whatsapp" if isinstance(config, WhatsAppConfig) else "telegram"
        if alias_store is not None:
            self._alias_store: Optional[ContactAliasStore] = alias_store
        elif hasattr(store, "_conn"):
            self._alias_store = ContactAliasStore(store._conn)
        else:
            self._alias_store = None
        self._echo_registry = echo_registry if echo_registry is not None else SelfEchoRegistry()
        self._owner_channel_handler = (
            owner_channel_handler
            if owner_channel_handler is not None
            else DefaultOwnerChannelHandler()
        )
        self._audit_writer = audit_writer
        if adapter is not None:
            self._adapter = adapter
        elif self._channel == "whatsapp":
            self._adapter = WhatsAppAdapter(max_message_length=config.max_message_length)
        else:
            self._adapter = TelegramAdapter(max_message_length=config.max_message_length)
        self._next_offset: Optional[int] = None
        self._running: bool = False
        self._last_draft_pipeline_notice: float = 0.0
        self._last_urgent_notice_by_contact: Dict[str, float] = {}

    def _audit_drop(self, reason: str, sender_id: str, chat_id: str) -> None:
        masked_sender = mask_phone_number(sender_id)
        masked_chat = mask_phone_number(chat_id)
        logger.info(
            "Audit drop: reason=%s, sender=%s, chat=%s",
            reason,
            masked_sender,
            masked_chat,
        )
        if self._audit_writer is not None:
            try:
                self._audit_writer.write(
                    event_type=AuditEventType.AUTHZ_DECISION,
                    actor="whatsapp_gateway",
                    target_entity_type="whatsapp_channel",
                    target_entity_id=masked_sender,
                    decision=AuditDecision.DENY,
                    policy_rule_id=reason,
                    telemetry={"reason": reason, "chat_id": masked_chat},
                )
            except Exception as exc:
                logger.error("Failed to write audit record for drop: %s", exc)

    def _audit_event(
        self,
        event_name: str,
        actor: str = "whatsapp_gateway",
        telemetry: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._audit_writer is not None:
            try:
                self._audit_writer.write(
                    event_type=AuditEventType.AUTHZ_DECISION,
                    actor=actor,
                    target_entity_type="whatsapp_channel",
                    target_entity_id="owner",
                    decision=AuditDecision.ALLOW,
                    policy_rule_id=event_name,
                    telemetry=telemetry or {},
                )
            except Exception as exc:
                logger.error("Failed to write audit event: %s", exc)

    @property
    def allowed_jids(self) -> List[str]:
        """Authoritative whitelist of allowed WhatsApp JIDs.

        Preference order:
        1. whatsapp_contact_settings in the database (single source of truth for CLI mutations)
        2. Fallback to static config.allowed_jids (initial seed or tests without DB contact settings)
        """
        if self._channel == "whatsapp" and hasattr(self._store, "_conn"):
            try:
                cur = self._store._conn.execute(
                    "SELECT contact_jid FROM whatsapp_contact_settings ORDER BY contact_jid"
                )
                db_jids = [r[0] for r in cur.fetchall()]
                if db_jids:
                    return db_jids
            except Exception:
                pass
        return getattr(self._config, "allowed_jids", [])

    @property
    def is_enabled(self) -> bool:
        """Whether the remote control gateway connector is enabled."""
        return self._config.enabled

    @property
    def next_offset(self) -> Optional[int]:
        """Current Telegram polling offset."""
        return self._next_offset

    def poll_and_process_once(self) -> List[ProcessResult]:
        """Perform a single polling cycle and process all received updates.

        Returns:
            List of ProcessResult records for each update in this cycle.
            Returns empty list if connector is disabled.
        """
        # Invariant: Disabled connector never polls
        if not self._config.enabled:
            return []

        if isinstance(self._config, WhatsAppConfig) and isinstance(
            self._transport, WhatsAppTransport
        ):
            if hasattr(self._transport, "get_events"):
                raw_updates = self._transport.get_events(limit=self._config.max_message_length)
            elif hasattr(self._transport, "get_updates"):
                raw_updates = self._transport.get_updates(limit=20)  # type: ignore[attr-defined]
            else:
                raw_updates = []
        elif isinstance(self._config, TelegramConfig) and isinstance(
            self._transport, TelegramTransport
        ):
            # Validate that the bot token environment variable exists
            token = os.environ.get(self._config.bot_token_env)
            if not token or not token.strip():
                raise ChannelConfigError(
                    f"Telegram gateway is enabled but environment variable "
                    f"'{self._config.bot_token_env}' is not set or empty."
                )

            # Long poll for updates via transport
            try:
                raw_updates = self._transport.get_updates(
                    offset=self._next_offset,
                    limit=self._config.max_poll_limit,
                    timeout=self._config.poll_timeout_seconds,
                )
            except TransportError as exc:
                logger.warning("Transport error during getUpdates: %s", exc)
                return []
        else:
            raw_updates = []

        results: List[ProcessResult] = []

        for raw in raw_updates:
            res = self._process_single_raw_update(raw)
            if res is not None:
                results.append(res)

        return results

    def _process_single_raw_update(self, raw_update: Dict[str, Any]) -> Optional[ProcessResult]:
        """Process an individual raw update through the security and routing pipeline."""
        raw_uid = raw_update.get("update_id")
        if isinstance(raw_uid, int):
            self._next_offset = max(self._next_offset or 0, raw_uid + 1)

        # 1. Update structure validation (human sender, private chat, length, zero-secret)
        is_valid, parsed, reject_reason = self._adapter.parse_update(raw_update)
        if not is_valid or parsed is None:
            # 2.7: Owner message directly to whitelisted contact (from_me=True, is_self_chat=False)
            # Store in contact's encrypted memory so context builder has both sides of conversation.
            if self._channel == "whatsapp" and self._contact_memory_store is not None:
                is_from_me = bool(raw_update.get("from_me") or (raw_update.get("key") and raw_update["key"].get("fromMe")))
                is_self = bool(raw_update.get("is_self_chat"))
                if is_from_me and not is_self:
                    target_chat = raw_update.get("chat_jid") or raw_update.get("remoteJid") or (raw_update.get("key") and raw_update["key"].get("remoteJid"))
                    out_text = raw_update.get("text")
                    if target_chat and out_text:
                        canonical_target = None
                        if CANONICAL_PN_REGEX.match(target_chat):
                            canonical_target = target_chat
                        elif target_chat.endswith("@lid") and self._alias_store:
                            canonical_target = self._alias_store.get_canonical_pn(target_chat)
                        if canonical_target and canonical_target in self.allowed_jids:
                            try:
                                self._contact_memory_store.store_memory(
                                    contact_jid=canonical_target,
                                    record_type="owner_message",
                                    content={
                                        "message_id": str(raw_uid or raw_update.get("id")),
                                        "text": out_text,
                                        "from_me": True,
                                        "chat_id": target_chat,
                                    },
                                )
                                last2 = canonical_target.split("@")[0][-2:]
                                self._audit_event(
                                    "owner_message_stored",
                                    actor="owner",
                                    telemetry={"contact": f"***{last2}", "text_length": len(out_text)},
                                )
                            except Exception as exc:
                                logger.warning("Failed to store owner message for %s: %s", canonical_target, exc)

            logger.warning("Inbound update %r rejected: %s", raw_uid, reject_reason)
            self._audit_drop(reject_reason or "validation_failed", "unknown", "unknown")
            return ProcessResult(
                update_id=str(raw_uid or "unknown"),
                channel_update_id=str(raw_uid or "unknown"),
                sender_id="unknown",
                state=UpdateLifecycleState.REJECTED,
                error_summary=reject_reason or "Update validation failed",
            )

        channel_update_id = parsed["channel_update_id"]
        sender_id_str = parsed["sender_id"]
        chat_id = parsed["chat_id"]
        text = parsed["text"]

        # 2. Authentication and Canonical Identity Resolution
        if self._channel == "whatsapp":
            message_id = parsed.get("message_id", "")
            # D5: Self-echo suppression
            if self._echo_registry and self._echo_registry.is_echo(message_id):
                logger.info("Dropping self-echo message id=%s", message_id)
                self._audit_drop("self_echo_suppressed", sender_id_str, chat_id)
                rec, _ = self._store.record_inbound_update(
                    channel=self._channel,
                    channel_update_id=channel_update_id,
                    sender_id=sender_id_str,
                    chat_id=chat_id,
                    raw_text=None,
                    state=UpdateLifecycleState.REJECTED,
                    metadata={"rejection_reason": "self_echo_suppressed"},
                )
                return ProcessResult(
                    update_id=rec.update_id,
                    channel_update_id=channel_update_id,
                    sender_id=sender_id_str,
                    state=UpdateLifecycleState.REJECTED,
                    error_summary="Self-echo message suppressed",
                )

            # D3/D4: from_me AND is_self_chat -> owner channel
            if parsed.get("is_owner"):
                configured_owner = getattr(self._config, "owner_jid", None)
                if configured_owner:
                    # Resolve identity of this self-chat event (after LID resolution)
                    sender_pn = parsed.get("sender_pn")
                    chat_jid = parsed.get("chat_jid", chat_id)
                    canonical_owner: Optional[str] = None
                    if sender_pn and CANONICAL_PN_REGEX.match(sender_pn):
                        canonical_owner = sender_pn
                    elif (chat_jid.endswith("@lid") or sender_id_str.endswith("@lid")) and self._alias_store:
                        lid = chat_jid if chat_jid.endswith("@lid") else sender_id_str
                        canonical_owner = self._alias_store.get_canonical_pn(lid)
                    elif CANONICAL_PN_REGEX.match(sender_id_str):
                        canonical_owner = sender_id_str

                    if not canonical_owner or canonical_owner.strip().lower() != configured_owner.strip().lower():
                        logger.warning(
                            "Self-chat owner command rejected: sender=%s resolved=%s does not match configured owner_jid=%s. Owner commands disabled.",
                            mask_phone_number(sender_id_str),
                            mask_phone_number(canonical_owner or "unresolved"),
                            mask_phone_number(configured_owner),
                        )
                        self._audit_drop("owner_identity_mismatch", sender_id_str, chat_id)
                        rec, _ = self._store.record_inbound_update(
                            channel=self._channel,
                            channel_update_id=channel_update_id,
                            sender_id=sender_id_str,
                            chat_id=chat_id,
                            raw_text=None,
                            state=UpdateLifecycleState.REJECTED,
                            metadata={"rejection_reason": "owner_identity_mismatch"},
                        )
                        return ProcessResult(
                            update_id=rec.update_id,
                            channel_update_id=channel_update_id,
                            sender_id=sender_id_str,
                            state=UpdateLifecycleState.REJECTED,
                            error_summary="owner_identity_mismatch",
                        )

                if self._owner_channel_handler:
                    self._owner_channel_handler.handle_owner_message(message_id, text)
                self._audit_event(
                    "owner_message_received",
                    actor="owner",
                    telemetry={"message_id": message_id, "text_length": len(text)},
                )
                rec, _ = self._store.record_inbound_update(
                    channel=self._channel,
                    channel_update_id=channel_update_id,
                    sender_id=sender_id_str,
                    chat_id=chat_id,
                    raw_text=None,
                    state=UpdateLifecycleState.COMPLETED,
                    metadata={"owner_action": "processed"},
                )
                return ProcessResult(
                    update_id=rec.update_id,
                    channel_update_id=channel_update_id,
                    sender_id=sender_id_str,
                    state=UpdateLifecycleState.COMPLETED,
                )

            # D1: Canonical Identity Resolution
            allowed_jids = self.allowed_jids
            sender_pn = parsed.get("sender_pn")
            chat_jid = parsed.get("chat_jid", chat_id)
            canonical_pn: Optional[str] = None

            if sender_pn and CANONICAL_PN_REGEX.match(sender_pn):
                canonical_pn = sender_pn
                if chat_jid.endswith("@lid") and self._alias_store:
                    self._alias_store.learn_alias(chat_jid, canonical_pn, allowed_jids)
            elif chat_jid.endswith("@lid") or sender_id_str.endswith("@lid"):
                lid = chat_jid if chat_jid.endswith("@lid") else sender_id_str
                if self._alias_store:
                    canonical_pn = self._alias_store.get_canonical_pn(lid)
            elif CANONICAL_PN_REGEX.match(sender_id_str):
                canonical_pn = sender_id_str

            if not canonical_pn:
                # D1: Unresolved identity - drop before key derivation or memory writes
                logger.warning(
                    "Unresolved identity for chat_jid=%s, sender_id=%s", chat_jid, sender_id_str
                )
                self._audit_drop("unresolved_identity", sender_id_str, chat_id)
                rec, _ = self._store.record_inbound_update(
                    channel=self._channel,
                    channel_update_id=channel_update_id,
                    sender_id=sender_id_str,
                    chat_id=chat_id,
                    raw_text=None,
                    state=UpdateLifecycleState.REJECTED,
                    metadata={"rejection_reason": "unresolved_identity"},
                )
                return ProcessResult(
                    update_id=rec.update_id,
                    channel_update_id=channel_update_id,
                    sender_id=sender_id_str,
                    state=UpdateLifecycleState.REJECTED,
                    error_summary="unresolved_identity",
                )

            # Whitelist check on canonical PN
            sender_authorized = canonical_pn in allowed_jids
            chat_authorized = True
            sender_id_str = canonical_pn
            try:
                numeric_sender_id = int(canonical_pn.split("@")[0])
            except (ValueError, IndexError):
                numeric_sender_id = abs(hash(canonical_pn)) % (10**9)
        elif isinstance(self._config, TelegramConfig):
            try:
                numeric_sender_id = int(sender_id_str)
            except ValueError:
                numeric_sender_id = -1

            try:
                numeric_chat_id = int(chat_id)
            except ValueError:
                numeric_chat_id = -1

            sender_authorized = numeric_sender_id in self._config.allowed_user_ids
            chat_authorized = (
                numeric_chat_id in self._config.allowed_chat_ids
                if self._config.allowed_chat_ids
                else True
            )
        else:
            sender_authorized = False
            chat_authorized = False

        if not sender_authorized or not chat_authorized:
            # Unauthorized user or chat: Reject immediately. Do NOT reply or enumerate tasks!
            reason = "UNAUTHORIZED_SENDER_ID" if not sender_authorized else "UNAUTHORIZED_CHAT_ID"
            logger.warning(
                "Unauthorized access attempt by sender_id=%s, chat_id=%s (reason=%s)",
                sender_id_str,
                chat_id,
                reason,
            )
            self._audit_drop(reason, sender_id_str, chat_id)
            # Record in store as REJECTED for security auditing (without raw text)
            rec, _ = self._store.record_inbound_update(
                channel=self._channel,
                channel_update_id=channel_update_id,
                sender_id=sender_id_str,
                chat_id=chat_id,
                raw_text=None,  # Do not persist unauthorized text
                state=UpdateLifecycleState.REJECTED,
                metadata={"rejection_reason": reason, "masked_sender": mask_phone_number(sender_id_str)},
            )
            return ProcessResult(
                update_id=rec.update_id,
                channel_update_id=channel_update_id,
                sender_id=sender_id_str,
                state=UpdateLifecycleState.REJECTED,
                error_summary="Unauthorized sender ID"
                if not sender_authorized
                else "Unauthorized chat ID",
            )

        # 3. Rate Limiting Check
        if not self._limiter.record_and_check(
            numeric_sender_id, limit=self._config.rate_limit_per_minute
        ):
            logger.warning("Rate limit exceeded for sender_id=%s", sender_id_str)
            rec, _ = self._store.record_inbound_update(
                channel=self._channel,
                channel_update_id=channel_update_id,
                sender_id=sender_id_str,
                chat_id=chat_id,
                raw_text=text,
                state=UpdateLifecycleState.REJECTED,
                metadata={"rejection_reason": "RATE_LIMIT_EXCEEDED"},
            )
            self._dispatch_outbound(
                recipient_id=chat_id,
                content="⚠️ Rate limit exceeded. Please wait a moment before sending more requests.",
                inbound_update_id=rec.update_id,
            )
            return ProcessResult(
                update_id=rec.update_id,
                channel_update_id=channel_update_id,
                sender_id=sender_id_str,
                state=UpdateLifecycleState.REJECTED,
                error_summary="Rate limit exceeded",
            )

        # 4. Durable Deduplication
        rec, is_duplicate = self._store.record_inbound_update(
            channel=self._channel,
            channel_update_id=channel_update_id,
            sender_id=sender_id_str,
            chat_id=chat_id,
            raw_text=text,
            state=UpdateLifecycleState.VALIDATED,
        )

        if is_duplicate:
            # If already processed or in-flight, skip duplicate task creation
            logger.info("Duplicate update %s skipped (state=%s)", channel_update_id, rec.state)
            return ProcessResult(
                update_id=rec.update_id,
                channel_update_id=channel_update_id,
                sender_id=sender_id_str,
                state=rec.state,
                task_id=rec.task_id,
                error_summary="Duplicate update skipped",
            )

        # 5. State Transition: VALIDATED -> QUEUED -> PROCESSING
        self._store.transition_update_state(rec.update_id, UpdateLifecycleState.QUEUED)
        self._store.transition_update_state(rec.update_id, UpdateLifecycleState.PROCESSING)

        # 6. Contact-Scoped Memory Isolation Retrieval & Inbound Processing for WhatsApp
        if self._channel == "whatsapp":
            configured_owner = getattr(self._config, "owner_jid", None)
            is_owner_sender = (
                (configured_owner and sender_id_str.lower() == configured_owner.lower())
                or (parsed.get("is_self_chat") and parsed.get("from_me"))
                or (configured_owner is None and isinstance(self._transport, SimulatedWhatsAppTransport) and self._contact_memory_store is None)
            )

            if not is_owner_sender:
                # 1.1 Non-owner contact message: MUST NEVER reach CommandRouter, central_brain, or ToolBroker
                # Store message in contact memory first if store is available
                if self._contact_memory_store is not None:
                    try:
                        self._contact_memory_store.store_memory(
                            contact_jid=sender_id_str,
                            record_type="inbound_message",
                            content={
                                "message_id": channel_update_id,
                                "text": text,
                                "sender_id": sender_id_str,
                                "chat_id": chat_id,
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to store contact memory for contact: %s", exc)

                # Check for crisis / urgent message: no draft, no model call, direct owner notice
                if is_urgent_message(text):
                    label = "Contact"
                    if self._draft_manager is not None:
                        try:
                            settings = self._draft_manager.get_contact_settings(sender_id_str)
                            label = settings.get("label") or "Contact"
                        except Exception:
                            pass
                    last2 = sender_id_str.split("@")[0][-2:] if "@" in sender_id_str else sender_id_str[-2:]

                    now_mono = time.monotonic()
                    last_notice_time = self._last_urgent_notice_by_contact.get(sender_id_str, 0.0)
                    is_rate_limited = (now_mono - last_notice_time) < 120.0

                    if not is_rate_limited:
                        self._last_urgent_notice_by_contact[sender_id_str] = now_mono
                        if self._supervisor is not None and hasattr(self._supervisor, "send_owner_notice"):
                            try:
                                self._supervisor.send_owner_notice(
                                    f"[AEGIS] URGENT: {label} (...{last2}) may need you. No draft prepared. Please read their message yourself and reply directly."
                                )
                            except Exception as exc:
                                logger.error("Failed to notify owner of urgent message: %s", exc)

                    self._audit_event(
                        "urgent_message_no_draft",
                        actor=sender_id_str,
                        telemetry={"rate_limited": is_rate_limited, "contact": f"***{last2}"},
                    )

                    self._store.transition_update_state(
                        rec.update_id,
                        UpdateLifecycleState.COMPLETED,
                    )
                    return ProcessResult(
                        update_id=rec.update_id,
                        channel_update_id=channel_update_id,
                        sender_id=sender_id_str,
                        state=UpdateLifecycleState.COMPLETED,
                        error_summary="urgent_message_no_draft",
                    )

                if self._draft_manager is None:
                    # Fail-closed: store nothing extra, audit reason, mark update FAILED.
                    self._audit_event(
                        "draft_pipeline_unavailable",
                        actor=sender_id_str,
                        telemetry={"reason": "draft_manager_missing"},
                    )
                    now_mono = time.monotonic()
                    if (now_mono - self._last_draft_pipeline_notice) >= 600:
                        self._last_draft_pipeline_notice = now_mono
                        if self._supervisor is not None and hasattr(self._supervisor, "send_owner_notice"):
                            try:
                                self._supervisor.send_owner_notice(
                                    "[AEGIS] Notice: WhatsApp draft pipeline is currently unavailable. Inbound contact messages are not being processed."
                                )
                            except Exception as exc:
                                logger.error("Failed to notify owner of draft pipeline unavailability: %s", exc)

                    self._store.transition_update_state(
                        rec.update_id,
                        UpdateLifecycleState.FAILED,
                        error_summary="draft_pipeline_unavailable",
                    )
                    return ProcessResult(
                        update_id=rec.update_id,
                        channel_update_id=channel_update_id,
                        sender_id=sender_id_str,
                        state=UpdateLifecycleState.FAILED,
                        error_summary="draft_pipeline_unavailable",
                    )

                # Draft pipeline is available: retrieve recent memory context
                contact_memories: List[Dict[str, Any]] = []
                if self._contact_memory_store is not None:
                    try:
                        contact_memories = self._contact_memory_store.list_contact_memories(
                            contact_jid=sender_id_str, limit=5
                        )
                    except Exception as exc:
                        logger.warning("Contact memory retrieval failed for contact: %s", exc)

                try:
                    draft_res = self._draft_manager.create_draft_for_inbound(
                        contact_jid=sender_id_str,
                        inbound_message_id=channel_update_id,
                        inbound_text=text,
                        recent_messages=contact_memories,
                    )
                    card = draft_res.get("card") if draft_res else None

                    # Deliver card to owner's self-chat via supervisor if available
                    if card and self._supervisor is not None and hasattr(self._supervisor, "send_owner_notice"):
                        try:
                            self._supervisor.send_owner_notice(card)
                        except Exception as exc:
                            logger.error("Failed to send approval card to owner: %s", exc)

                    # Mark inbound update as COMPLETED
                    self._store.transition_update_state(
                        rec.update_id,
                        UpdateLifecycleState.COMPLETED,
                    )
                    return ProcessResult(
                        update_id=rec.update_id,
                        channel_update_id=channel_update_id,
                        sender_id=sender_id_str,
                        state=UpdateLifecycleState.COMPLETED,
                        response_text=card,
                    )
                except Exception as exc:
                    logger.error("Failed to generate draft for inbound message %s: %s", rec.update_id, exc)
                    self._audit_event(
                        "draft_pipeline_unavailable",
                        actor=sender_id_str,
                        telemetry={"reason": "draft_generation_failed"},
                    )
                    now_mono = time.monotonic()
                    if (now_mono - self._last_draft_pipeline_notice) >= 600:
                        self._last_draft_pipeline_notice = now_mono
                        if self._supervisor is not None and hasattr(self._supervisor, "send_owner_notice"):
                            try:
                                self._supervisor.send_owner_notice(
                                    "[AEGIS] Notice: WhatsApp draft pipeline error. Inbound message could not be drafted."
                                )
                            except Exception as exc_notify:
                                logger.error("Failed to notify owner: %s", exc_notify)

                    self._store.transition_update_state(
                        rec.update_id,
                        UpdateLifecycleState.FAILED,
                        error_summary="draft_pipeline_unavailable",
                    )
                    return ProcessResult(
                        update_id=rec.update_id,
                        channel_update_id=channel_update_id,
                        sender_id=sender_id_str,
                        state=UpdateLifecycleState.FAILED,
                        error_summary="draft_pipeline_unavailable",
                    )

        # 7. Route Command & Execute (For non-WhatsApp channels e.g. Telegram)
        try:
            router_res = self._router.route(
                text, sender_id=sender_id_str, chat_id=chat_id, channel=self._channel
            )
        except Exception as exc:
            logger.error("Command execution error for update %s: %s", rec.update_id, exc)
            self._store.transition_update_state(
                rec.update_id,
                UpdateLifecycleState.FAILED,
                error_summary=str(exc),
            )
            self._dispatch_outbound(
                recipient_id=chat_id,
                content=f"An error occurred while processing your request: {exc}",
                inbound_update_id=rec.update_id,
            )
            return ProcessResult(
                update_id=rec.update_id,
                channel_update_id=channel_update_id,
                sender_id=sender_id_str,
                state=UpdateLifecycleState.FAILED,
                error_summary=str(exc),
            )

        # 7. Final State Transition for Inbound Update
        final_state = (
            UpdateLifecycleState.AWAITING_APPROVAL
            if router_res.awaiting_approval
            else UpdateLifecycleState.COMPLETED
        )
        self._store.transition_update_state(
            rec.update_id,
            final_state,
            task_id=router_res.task_id,
        )

        # 8. Outbound Response Dispatch
        self._dispatch_outbound(
            recipient_id=chat_id,
            content=router_res.response_text,
            inbound_update_id=rec.update_id,
            task_id=router_res.task_id,
        )

        return ProcessResult(
            update_id=rec.update_id,
            channel_update_id=channel_update_id,
            sender_id=sender_id_str,
            state=final_state,
            response_text=router_res.response_text,
            task_id=router_res.task_id,
        )

    def _dispatch_outbound(
        self,
        recipient_id: str,
        content: str,
        inbound_update_id: Optional[str] = None,
        task_id: Optional[str] = None,
        requires_approval: bool = False,
        approval_id: Optional[str] = None,
    ) -> None:
        """Deliver response chunks via transport.

        Ensures outbound failure cannot corrupt task state.
        Validates that recipient_id is an authorized private chat and not a group/channel.
        Enforces exact-content and exact-recipient approval check when requires_approval is True.
        """
        # Validate recipient destination
        is_group = False
        try:
            int_recip: Optional[int] = int(recipient_id)
            if int_recip is not None and int_recip <= 0:
                is_group = True
        except ValueError:
            int_recip = None
            if (
                "@g.us" in recipient_id
                or "@broadcast" in recipient_id
                or "group" in recipient_id.lower()
            ):
                is_group = True

        if is_group:
            logger.error(
                "Outbound dispatch rejected: destination %s is a group or channel.", recipient_id
            )
            out_rec = self._store.record_outbound_message(
                channel=self._channel,
                recipient_id=recipient_id,
                content=content[:100],
                inbound_update_id=inbound_update_id,
                task_id=task_id,
            )
            self._store.update_outbound_status(
                out_rec.message_id,
                OutboundStatus.FAILED,
                error_message=(
                    f"Group and channel destinations ({recipient_id}) are strictly prohibited."
                ),
            )
            return

        # Invariant: Strict receive-only mode prohibits outbound WhatsApp sends
        if self._channel == "whatsapp" and getattr(self._config, "receive_only", False):
            logger.info(
                "Outbound WhatsApp dispatch suppressed: receive-only for %s",
                recipient_id,
            )
            out_rec = self._store.record_outbound_message(
                channel=self._channel,
                recipient_id=recipient_id,
                content=content[:100],
                inbound_update_id=inbound_update_id,
                task_id=task_id,
            )
            self._store.update_outbound_status(
                out_rec.message_id,
                OutboundStatus.FAILED,
                error_message=(
                    "Outbound WhatsApp sending is strictly disabled in receive-only mode."
                ),
            )
            return

        # Check destination allowlist
        if self._channel == "whatsapp":
            allowed_jids = getattr(self._config, "allowed_jids", [])
            if allowed_jids and recipient_id not in allowed_jids:
                logger.error(
                    "Outbound dispatch rejected: destination %s not in allowed_jids.", recipient_id
                )
                out_rec = self._store.record_outbound_message(
                    channel=self._channel,
                    recipient_id=recipient_id,
                    content=content[:100],
                    inbound_update_id=inbound_update_id,
                    task_id=task_id,
                )
                self._store.update_outbound_status(
                    out_rec.message_id,
                    OutboundStatus.FAILED,
                    error_message=f"Destination {recipient_id} is not in allowed_jids.",
                )
                return
        else:
            allowed_chat_ids = getattr(self._config, "allowed_chat_ids", [])
            if allowed_chat_ids and int_recip is not None and int_recip not in allowed_chat_ids:
                logger.error(
                    "Outbound dispatch rejected: destination %s not in allowed_chat_ids.",
                    recipient_id,
                )
                out_rec = self._store.record_outbound_message(
                    channel=self._channel,
                    recipient_id=recipient_id,
                    content=content[:100],
                    inbound_update_id=inbound_update_id,
                    task_id=task_id,
                )
                self._store.update_outbound_status(
                    out_rec.message_id,
                    OutboundStatus.FAILED,
                    error_message=f"Destination {recipient_id} is not in allowed_chat_ids.",
                )
                return

        # Approval check if required
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        is_autonomous = inbound_update_id is None
        if requires_approval or (
            self._channel == "whatsapp"
            and getattr(self._config, "require_outbound_approval", False)
            and is_autonomous
        ):
            approval_valid = False
            approval_err = "Approval required"
            if not approval_id:
                approval_err = "Outbound dispatch requires explicit human approval ID."
            elif not self._approval_store:
                approval_err = "Trust Core ApprovalStore is unavailable."
            else:
                req = self._approval_store.get_request(approval_id)
                if not req:
                    approval_err = f"Approval request '{approval_id}' not found."
                elif req.status != "APPROVED":
                    approval_err = (
                        f"Approval request '{approval_id}' has non-approved status '{req.status}'."
                    )
                elif req.is_expired():
                    approval_err = f"Approval request '{approval_id}' is expired."
                elif req.target_resource != recipient_id and req.target_resource != "*":
                    approval_err = (
                        f"Approval target mismatch: approved for '{req.target_resource}', "
                        f"attempted send to '{recipient_id}'."
                    )
                elif content_hash not in req.parameters_summary:
                    approval_err = (
                        "Approval content mismatch: message content was modified after approval."
                    )
                else:
                    approval_valid = True

            if not approval_valid:
                logger.error("Outbound approval validation failed: %s", approval_err)
                out_rec = self._store.record_outbound_message(
                    channel=self._channel,
                    recipient_id=recipient_id,
                    content=content[:100],
                    inbound_update_id=inbound_update_id,
                    task_id=task_id,
                )
                self._store.update_outbound_status(
                    out_rec.message_id,
                    OutboundStatus.FAILED,
                    error_message=approval_err,
                )
                return

        chunks = self._adapter.format_outbound_chunks(content)

        for chunk in chunks:
            out_rec = self._store.record_outbound_message(
                channel=self._channel,
                recipient_id=recipient_id,
                content=chunk,
                inbound_update_id=inbound_update_id,
                task_id=task_id,
            )

            try:
                if isinstance(self._transport, WhatsAppTransport):
                    self._transport.send_message(
                        recipient_jid=recipient_id,
                        text=chunk,
                        correlation_id=inbound_update_id,
                        approval_id=approval_id,
                        approval_store=self._approval_store,
                        require_approval=requires_approval,
                    )
                elif isinstance(self._transport, TelegramTransport):
                    self._transport.send_message(chat_id=recipient_id, text=chunk)
                else:
                    self._transport.send_message(recipient_id, chunk)  # type: ignore[call-arg]
                self._store.update_outbound_status(out_rec.message_id, OutboundStatus.SENT)
            except Exception as exc:
                logger.error("Outbound delivery to %s failed: %s", recipient_id, exc)
                # Mark as UNCERTAIN or FAILED; do NOT touch or rollback SAGE task state!
                self._store.update_outbound_status(
                    out_rec.message_id,
                    OutboundStatus.UNCERTAIN,
                    error_message=str(exc),
                )

    def reconcile_startup(self) -> Dict[str, Any]:
        """Reconcile in-flight updates and pending outbounds after system restart.

        Invariants:
        - Never automatically re-executes uncertain external actions.
        - Idempotent: repeated runs produce identical safe states.
        - Reconciles updates in PROCESSING or QUEUED without duplicating side effects.
        """
        report: Dict[str, Any] = {
            "updates_checked": 0,
            "updates_completed": 0,
            "updates_flagged_recovery": 0,
            "outbound_marked_uncertain": 0,
        }

        # 1. Reconcile in-flight inbound updates (PROCESSING, QUEUED)
        in_flight_states = [UpdateLifecycleState.PROCESSING, UpdateLifecycleState.QUEUED]
        for state in in_flight_states:
            updates = self._store.list_inbound_updates(
                channel=self._channel, state=state, limit=100
            )
            for upd in updates:
                report["updates_checked"] += 1
                if upd.task_id:
                    task = self._router._supervisor._store.get_task(upd.task_id)
                    if task and task.state.is_terminal:
                        if task.state == TaskState.COMPLETED:
                            self._store.transition_update_state(
                                upd.update_id, UpdateLifecycleState.COMPLETED, task_id=upd.task_id
                            )
                            report["updates_completed"] += 1
                        else:
                            self._store.transition_update_state(
                                upd.update_id,
                                UpdateLifecycleState.FAILED,
                                error_summary=f"Task terminated in state {task.state.value}",
                                task_id=upd.task_id,
                            )
                            report["updates_flagged_recovery"] += 1
                    elif task and task.state == TaskState.AWAITING_APPROVAL:
                        self._store.transition_update_state(
                            upd.update_id,
                            UpdateLifecycleState.AWAITING_APPROVAL,
                            task_id=upd.task_id,
                        )
                    else:
                        # Task was executing or submitted; cannot prove side effects did not occur
                        self._store.transition_update_state(
                            upd.update_id,
                            UpdateLifecycleState.FAILED,
                            error_summary=(
                                "Interrupted by system restart during execution; recovery required"
                            ),
                            task_id=upd.task_id,
                        )
                        report["updates_flagged_recovery"] += 1
                else:
                    # No task created. Check if outbound reply was sent
                    outbounds = self._store.list_outbound_messages(channel=self._channel, limit=100)
                    matching = [o for o in outbounds if o.inbound_update_id == upd.update_id]
                    if matching and any(o.status == OutboundStatus.SENT for o in matching):
                        self._store.transition_update_state(
                            upd.update_id, UpdateLifecycleState.COMPLETED
                        )
                        report["updates_completed"] += 1
                    else:
                        self._store.transition_update_state(
                            upd.update_id,
                            UpdateLifecycleState.FAILED,
                            error_summary="Interrupted by system restart before completion",
                        )
                        report["updates_flagged_recovery"] += 1

        # 2. Reconcile unfinished outbound messages (PENDING)
        pending_outbounds = self._store.list_outbound_messages(
            channel=self._channel, status=OutboundStatus.PENDING, limit=100
        )
        for out_msg in pending_outbounds:
            self._store.update_outbound_status(
                out_msg.message_id,
                OutboundStatus.UNCERTAIN,
                error_message="Interrupted by system restart while pending dispatch",
            )
            report["outbound_marked_uncertain"] += 1

        return report

    def start_polling(self, max_iterations: Optional[int] = None) -> None:
        """Start polling loop for updates."""
        self._running = True
        iterations = 0
        while self._running:
            self.poll_and_process_once()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
