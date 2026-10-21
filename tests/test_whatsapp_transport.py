"""Phase 9C.2 Simulated WhatsApp Transport and Sidecar IPC Test Suite.

Canonical Reference:
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md
    PROMPT 035: Phase 9C.2 Simulated WhatsApp Transport Foundation

Test Coverage Areas:
1. Normal private-message ingestion (/status, /task, /help).
2. Unknown-contact rejection (unauthorized JID rejected silently).
3. Inbound group and broadcast rejection (@g.us, @broadcast).
4. Duplicate event handling (durable deduplication).
5. Malformed event rejection (bad JSON, missing fields, placeholder requestId).
6. Reconnect and replay scenarios.
7. Restart persistence across process lifecycle.
8. Outbound group/broadcast destination rejection.
9. Exact-content and exact-recipient approval binding.
10. Invalidation on tamper (editing content or recipient JID).
11. Uncertain outbound delivery (TCP drop before server ACK).
12. Interrupted task recovery (reconcile_startup marks orphaned PROCESSING as FAILED).
13. Secret redaction in transport and logs.
14. Protocol envelope validation (schema, limits, malformed lines).

100% Offline Guarantee: Zero network calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from src.config.models import WhatsAppConfig
from src.gateway.errors import TransportError
from src.gateway.gateway import ChannelGateway
from src.gateway.models import (
    UpdateLifecycleState,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import CommandRouter
from src.gateway.sidecar_protocol import (
    SidecarEnvelope,
    SidecarEventType,
    SidecarProtocolError,
    parse_sidecar_line,
    serialize_sidecar_envelope,
)
from src.gateway.store import GatewayStore
from src.gateway.whatsapp import WhatsAppAdapter
from src.gateway.whatsapp_transport import SimulatedWhatsAppTransport
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.skills.first_party import register_first_party_skills
from src.skills.registry import SkillRegistry
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.approval import ApprovalStore
from src.trust.policy import AutonomyTier

OWNER_JID = "15551234567@s.whatsapp.net"
STRANGER_JID = "19999999999@s.whatsapp.net"
GROUP_JID = "123456789012345678@g.us"
BROADCAST_JID = "status@broadcast"


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    """Isolated runtime database with migrations applied."""
    db_file = tmp_path / "test_whatsapp_gateway.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Isolated workspace scratch directory."""
    sb = tmp_path / "workspace"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "test_file.txt").write_text("WhatsApp Gateway Test File", encoding="utf-8")
    return sb


@pytest.fixture
def whatsapp_env(test_db: sqlite3.Connection, sandbox: Path):
    """Build a complete local SAGE environment wired to ChannelGateway with Simulated transport."""
    authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])

    registry = SkillRegistry()
    register_first_party_skills(registry, broker)

    state_store = StateStore(test_db)
    approval_store = ApprovalStore(test_db)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=authority,
        tool_broker=broker,
        skill_registry=registry,
    )

    gateway_store = GatewayStore(test_db)
    transport = SimulatedWhatsAppTransport(store=approval_store)
    router = CommandRouter(
        supervisor=supervisor,
        tool_broker=broker,
        trust_authority=authority,
        approval_store=approval_store,
        workspace_alias="whatsapp_workspace",
        workspace_dir=sandbox,
    )

    config = WhatsAppConfig(
        enabled=True,
        allowed_jids=[OWNER_JID],
        session_path=str(sandbox / "wa_session"),
        max_message_length=4096,
        rate_limit_per_minute=30,
        require_outbound_approval=True,
    )
    limiter = SlidingWindowRateLimiter()
    gateway = ChannelGateway(
        config=config,
        store=gateway_store,
        transport=transport,
        router=router,
        rate_limiter=limiter,
        approval_store=approval_store,
    )

    return {
        "gateway": gateway,
        "transport": transport,
        "store": gateway_store,
        "supervisor": supervisor,
        "state_store": state_store,
        "approval_store": approval_store,
        "router": router,
        "limiter": limiter,
        "config": config,
        "sandbox": sandbox,
        "test_db": test_db,
    }


# =====================================================================
# 1. Normal Private-Message Ingestion
# =====================================================================


def test_whatsapp_status_command_ingestion(whatsapp_env):
    """Valid /status text from authorized owner JID reaches router and succeeds."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/status",
        message_id="msg_status_001",
    )

    results = gateway.poll_and_process_once()
    assert len(results) == 1
    res = results[0]
    assert res.state == UpdateLifecycleState.COMPLETED
    assert res.response_text is not None
    assert "SAGE System Status" in res.response_text
    assert len(transport.sent_messages) == 1


def test_whatsapp_task_command_ingestion(whatsapp_env):
    """Valid /task text from owner JID creates supervisor task and requires confirmation."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/task read test_file.txt",
        message_id="msg_task_001",
    )

    results = gateway.poll_and_process_once()
    assert len(results) == 1
    res = results[0]
    assert res.state == UpdateLifecycleState.AWAITING_APPROVAL
    assert res.task_id is not None
    assert "Briefing" in (res.response_text or "")

    # Confirm the proposed task briefing
    transport.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text=f"/confirm {res.task_id}",
        message_id="msg_confirm_001",
    )
    confirm_results = gateway.poll_and_process_once()
    assert len(confirm_results) == 1
    assert confirm_results[0].state == UpdateLifecycleState.COMPLETED


def test_whatsapp_help_command_ingestion(whatsapp_env):
    """Valid /help text from authorized owner JID returns available commands."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/help",
        message_id="msg_help_001",
    )

    results = gateway.poll_and_process_once()
    assert len(results) == 1
    res = results[0]
    assert res.state == UpdateLifecycleState.COMPLETED
    assert res.response_text is not None
    assert "/status" in res.response_text


# =====================================================================
# 2. Unknown-Contact Rejection
# =====================================================================


def test_whatsapp_stranger_rejection(whatsapp_env):
    """Inbound message from non-allowlisted JID is rejected silently without task creation."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.enqueue_synthetic_message(
        sender_jid=STRANGER_JID,
        text="/status",
        message_id="msg_stranger_001",
    )

    results = gateway.poll_and_process_once()
    assert len(results) == 1
    res = results[0]
    assert res.state == UpdateLifecycleState.REJECTED
    assert "unauthorized" in (res.error_summary or "").lower()
    assert len(transport.sent_messages) == 0


# =====================================================================
# 3. Inbound Group and Broadcast Rejection
# =====================================================================


def test_whatsapp_inbound_group_rejection(whatsapp_env):
    """Messages from group JID (@g.us) are rejected before execution."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.enqueue_synthetic_message(
        sender_jid=GROUP_JID,
        text="/status",
        message_id="msg_group_001",
        conversation_type="group",
    )

    results = gateway.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "Group and broadcast" in (results[0].error_summary or "")
    assert len(transport.sent_messages) == 0


def test_whatsapp_inbound_broadcast_rejection(whatsapp_env):
    """Messages from broadcast JID (@broadcast) are rejected before execution."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.enqueue_synthetic_message(
        sender_jid=BROADCAST_JID,
        text="/status",
        message_id="msg_broadcast_001",
        conversation_type="broadcast",
    )

    results = gateway.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "Group and broadcast" in (results[0].error_summary or "")
    assert len(transport.sent_messages) == 0


def test_whatsapp_inbound_spoofed_group_metadata_rejection(whatsapp_env):
    """Even if sender JID looks individual, metadata indicating group chat causes rejection."""
    adapter = WhatsAppAdapter()
    raw = {
        "key": {
            "remoteJid": OWNER_JID,
            "id": "spoof_01",
            "participant": "123456789012345678@g.us",
        },
        "chat_type": "group",
        "message": {"conversation": "/status"},
    }
    is_valid, parsed, reason = adapter.parse_update(raw)
    assert is_valid is False
    assert parsed is None
    assert "Group" in (reason or "")


# =====================================================================
# 4. Duplicate Event Handling
# =====================================================================


def test_whatsapp_duplicate_event_deduplication(whatsapp_env):
    """Replaying the same message ID is deduplicated via gateway store."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/status",
        message_id="msg_dup_100",
    )

    results1 = gateway.poll_and_process_once()
    assert len(results1) == 1
    assert results1[0].state == UpdateLifecycleState.COMPLETED

    # Re-enqueue the exact same message ID
    transport.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/status",
        message_id="msg_dup_100",
    )

    results2 = gateway.poll_and_process_once()
    assert len(results2) == 1
    assert results2[0].state == UpdateLifecycleState.COMPLETED
    assert results2[0].error_summary == "Duplicate update skipped"


# =====================================================================
# 5. Malformed Event Rejection
# =====================================================================


def test_whatsapp_malformed_event_rejection(whatsapp_env):
    """Malformed events, missing fields, or empty messages are safely rejected or dropped."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    # 1. Missing message body
    transport.enqueue_raw_event(
        {
            "key": {"remoteJid": OWNER_JID, "id": "malformed_001"},
            "message": {},
        }
    )
    results = gateway.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED

    # 2. CVE-2026-48063 placeholder requestId payload
    transport.enqueue_raw_event(
        {
            "key": {"remoteJid": OWNER_JID, "id": "malformed_cve_002"},
            "requestId": "fake_placeholder_history_sync",
            "message": {"conversation": "/status"},
        }
    )
    results_cve = gateway.poll_and_process_once()
    assert len(results_cve) == 1
    assert results_cve[0].state == UpdateLifecycleState.REJECTED
    assert "CVE-2026-48063" in (results_cve[0].error_summary or "")


# =====================================================================
# 6. Reconnect and Replay Scenarios
# =====================================================================


def test_whatsapp_reconnect_and_offline_queue_processing(whatsapp_env):
    """Disconnecting transport pauses polling; reconnect processes pending offline events."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport.disconnect()
    assert transport.is_connected is False

    # Enqueue offline message
    transport.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/status",
        message_id="msg_offline_001",
    )
    with pytest.raises(TransportError):
        gateway.poll_and_process_once()

    # Reconnect transport
    transport.connect()
    assert transport.is_connected is True

    # Polling now succeeds and processes the queued offline message
    results = gateway.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.COMPLETED


# =====================================================================
# 7. Restart Persistence Across Process Lifecycle
# =====================================================================


def test_whatsapp_restart_persistence(whatsapp_env):
    """Inbound update records survive gateway teardown and reconstruction."""
    test_db = whatsapp_env["test_db"]
    gateway1: ChannelGateway = whatsapp_env["gateway"]
    transport1: SimulatedWhatsAppTransport = whatsapp_env["transport"]

    transport1.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/status",
        message_id="msg_persist_001",
    )
    results = gateway1.poll_and_process_once()
    assert results[0].state == UpdateLifecycleState.COMPLETED

    # Simulate process restart by constructing a fresh gateway with the same DB
    store2 = GatewayStore(test_db)
    transport2 = SimulatedWhatsAppTransport(store=whatsapp_env["approval_store"])
    gateway2 = ChannelGateway(
        config=whatsapp_env["config"],
        store=store2,
        transport=transport2,
        router=whatsapp_env["router"],
        rate_limiter=whatsapp_env["limiter"],
        approval_store=whatsapp_env["approval_store"],
    )

    # Verify duplicate detection across restarts
    transport2.enqueue_synthetic_message(
        sender_jid=OWNER_JID,
        text="/status",
        message_id="msg_persist_001",
    )
    dup_results = gateway2.poll_and_process_once()
    assert len(dup_results) == 1
    assert dup_results[0].error_summary == "Duplicate update skipped"


# =====================================================================
# 8. Outbound Group and Broadcast Destination Rejection
# =====================================================================


def test_whatsapp_outbound_group_rejection(whatsapp_env):
    """Attempting outbound send to @g.us raises TransportError before transmission."""
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]
    with pytest.raises(TransportError, match="Group destinations are strictly rejected"):
        transport.send_message(recipient_jid=GROUP_JID, text="Hello group")


def test_whatsapp_outbound_broadcast_rejection(whatsapp_env):
    """Attempting outbound send to @broadcast raises TransportError before transmission."""
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]
    with pytest.raises(TransportError, match="Group destinations are strictly rejected"):
        transport.send_message(recipient_jid=BROADCAST_JID, text="Hello broadcast")


# =====================================================================
# 9. Exact-Content and Exact-Recipient Approval Binding
# =====================================================================


def test_whatsapp_exact_content_approval_binding_success(whatsapp_env):
    """Outbound message succeeds when approval matches recipient JID and content SHA-256."""
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]
    approval_store: ApprovalStore = whatsapp_env["approval_store"]

    text = "Detailed automated report from SAGE."
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    req = approval_store.create_request(
        action_name="channel_outbound_message",
        target_resource=OWNER_JID,
        parameters_summary=f"content_hash={content_hash}",
    )
    approval_store.approve_request(request_id=req.request_id, actor="admin_operator")

    res = transport.send_message(
        recipient_jid=OWNER_JID,
        text=text,
        require_approval=True,
        approval_id=req.request_id,
    )
    assert res["status"] == "SENT"
    assert len(transport.sent_messages) == 1
    assert transport.sent_messages[0]["text"] == text


# =====================================================================
# 10. Invalidation on Tamper (Content or Recipient Alteration)
# =====================================================================


def test_whatsapp_approval_tamper_content_rejected(whatsapp_env):
    """Altering approved message content fails closed with invalid approval."""
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]
    approval_store: ApprovalStore = whatsapp_env["approval_store"]

    original_text = "Benign report text."
    tampered_text = "Malicious altered payload."
    original_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()

    req = approval_store.create_request(
        action_name="channel_outbound_message",
        target_resource=OWNER_JID,
        parameters_summary=f"content_hash={original_hash}",
    )
    approval_store.approve_request(request_id=req.request_id, actor="admin_operator")

    # Attempt to send tampered text with the original approval ID
    with pytest.raises(TransportError, match="hash mismatch"):
        transport.send_message(
            recipient_jid=OWNER_JID,
            text=tampered_text,
            require_approval=True,
            approval_id=req.request_id,
        )
    assert len(transport.sent_messages) == 0


def test_whatsapp_approval_tamper_recipient_rejected(whatsapp_env):
    """Altering approved recipient JID fails closed with invalid approval."""
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]
    approval_store: ApprovalStore = whatsapp_env["approval_store"]

    text = "Confidential report text."
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    req = approval_store.create_request(
        action_name="channel_outbound_message",
        target_resource=OWNER_JID,
        parameters_summary=f"content_hash={content_hash}",
    )
    approval_store.approve_request(request_id=req.request_id, actor="admin_operator")

    # Attempt to send to STRANGER_JID using OWNER_JID's approval
    with pytest.raises(TransportError, match="target mismatch"):
        transport.send_message(
            recipient_jid=STRANGER_JID,
            text=text,
            require_approval=True,
            approval_id=req.request_id,
        )
    assert len(transport.sent_messages) == 0


# =====================================================================
# 11. Uncertain Outbound Delivery
# =====================================================================


def test_whatsapp_uncertain_outbound_delivery(whatsapp_env):
    """Simulated transport drop before server ACK transitions outbound status to UNCERTAIN."""
    transport: SimulatedWhatsAppTransport = whatsapp_env["transport"]
    transport.set_uncertain_delivery(True)

    with pytest.raises(TransportError, match="uncertain delivery"):
        transport.send_message(
            recipient_jid=OWNER_JID,
            text="Critical notification with uncertain network ACK.",
            require_approval=False,
        )


# =====================================================================
# 12. Interrupted Task Recovery
# =====================================================================


def test_whatsapp_interrupted_task_recovery(whatsapp_env):
    """reconcile_startup marks orphaned PROCESSING updates as FAILED."""
    gateway: ChannelGateway = whatsapp_env["gateway"]
    store: GatewayStore = whatsapp_env["store"]

    # Manually insert an orphaned update in PROCESSING state
    upd, _ = store.record_inbound_update(
        channel="whatsapp",
        channel_update_id="wa_orphan_001",
        sender_id=OWNER_JID,
        chat_id=OWNER_JID,
        raw_text="/task long_running_task",
        state=UpdateLifecycleState.PROCESSING,
    )

    reconciled = gateway.reconcile_startup()
    assert reconciled["updates_flagged_recovery"] >= 1

    record = store.get_update_by_channel_id("whatsapp", "wa_orphan_001")
    assert record is not None
    assert record.state == UpdateLifecycleState.FAILED


# =====================================================================
# 13. Secret Redaction in Transport and Logs
# =====================================================================


def test_whatsapp_secret_redaction(whatsapp_env):
    """Zero secrets in logged events or outbound payloads."""
    adapter = WhatsAppAdapter()
    text_with_token = "Here is my secret bot token: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ12345678"
    raw_event = {
        "key": {"remoteJid": OWNER_JID, "id": "secret_001"},
        "message": {"conversation": text_with_token},
    }
    is_valid, parsed, reason = adapter.parse_update(raw_event)
    assert is_valid is False
    assert "zero-secret" in (reason or "").lower()


# =====================================================================
# 14. Protocol Envelope Validation
# =====================================================================


def test_sidecar_protocol_envelope_validation():
    """Serialization, deserialization, oversized lines, and schema limits."""
    # 1. Valid serialization & deserialization
    envelope = SidecarEnvelope(
        type=SidecarEventType.INBOUND_MESSAGE,
        payload={"jid": OWNER_JID, "text": "Hello SAGE"},
    )
    raw_line = serialize_sidecar_envelope(envelope)
    assert raw_line.endswith("\n")

    parsed = parse_sidecar_line(raw_line)
    assert parsed.version == "1.0"
    assert parsed.type == SidecarEventType.INBOUND_MESSAGE
    assert parsed.payload["jid"] == OWNER_JID

    # 2. Oversized line rejection (>64KB)
    huge_line = "x" * 65537 + "\n"
    with pytest.raises(SidecarProtocolError, match="exceeded maximum length"):
        parse_sidecar_line(huge_line)

    # 3. Invalid JSON rejection
    with pytest.raises(SidecarProtocolError, match="Malformed JSON"):
        parse_sidecar_line("not-valid-json\n")

    # 4. Missing required envelope fields
    with pytest.raises(SidecarProtocolError):
        parse_sidecar_line(json.dumps({"event": "ping"}) + "\n")

    # 5. Zero-secret enforcement in envelope creation
    with pytest.raises(ValueError, match="zero-secret"):
        SidecarEnvelope(
            type=SidecarEventType.OUTBOUND_SEND,
            payload={"text": "sk-1234567890abcdef1234567890abcdef"},
        )
