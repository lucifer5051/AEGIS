"""Tests for WhatsApp Urgent and Crisis Message Detection and Triage.

Canonical Reference:
    AEGIS Prompt 9 / T3 Urgent / Crisis Messages
    config/whatsapp_urgent_phrases.txt
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from src.gateway.gateway import ChannelGateway
from src.gateway.local_draft_provider import is_urgent_message, load_urgent_phrases
from src.gateway.models import UpdateLifecycleState
from src.gateway.whatsapp_transport import SimulatedWhatsAppTransport
from src.gateway.drafts import DraftManager
from src.trust.audit import AuditDecision, AuditEventType, AuditWriter


class FailingDraftProvider:
    """DraftProvider that raises an AssertionError if called."""

    def draft(self, ctx: Any) -> Any:
        raise AssertionError("Model draft provider must NEVER be called for urgent messages!")


class CapturingAuditWriter(AuditWriter):
    """Captures all audit events for verification."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def write(
        self,
        event_type: AuditEventType,
        actor: str,
        target_entity_type: str,
        target_entity_id: str,
        decision: AuditDecision,
        policy_rule_id: str,
        telemetry: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "actor": actor,
                "target_entity_type": target_entity_type,
                "target_entity_id": target_entity_id,
                "decision": decision,
                "policy_rule_id": policy_rule_id,
                "telemetry": telemetry or {},
            }
        )


class CapturingSupervisor:
    """Mock supervisor that captures owner notices."""

    def __init__(self) -> None:
        self.notices: List[str] = []

    def send_owner_notice(self, text: str) -> bool:
        self.notices.append(text)
        return True


# -----------------------------------------------------------------------------
# 1. Linguistic Corpus Coverage (5 Language Forms)
# -----------------------------------------------------------------------------

def test_urgent_corpus_all_five_language_forms() -> None:
    """Assert crisis/urgent detection across EN, HI-Roman, HI-Devanagari, MR-Roman, MR-Devanagari."""
    patterns = load_urgent_phrases()

    # English
    assert is_urgent_message("I am having severe chest pain, please call an ambulance", patterns)
    assert is_urgent_message("Someone is breaking in, I am under attack", patterns)
    assert is_urgent_message("Please call me urgently, cannot breathe", patterns)
    assert is_urgent_message("I want to end my life, please help", patterns)

    # Hindi (Roman / Hinglish)
    assert is_urgent_message("bhai turant call karo bohot bada accident ho gaya", patterns)
    assert is_urgent_message("seene me dard ho raha hai saans nahi aa rahi", patterns)
    assert is_urgent_message("emergency hai call karo jaldi", patterns)
    assert is_urgent_message("koi ghar me ghus gaya bachao", patterns)

    # Hindi (Devanagari)
    assert is_urgent_message("सीने में दर्द हो रहा है तुरंत कॉल करो", patterns)
    assert is_urgent_message("एंबुलेंस बुलाओ बहुत खून बह रहा है", patterns)
    assert is_urgent_message("सांस नहीं आ रही है मदद करो", patterns)
    assert is_urgent_message("कोई घर में घुस गया बचाओ मुझे", patterns)

    # Marathi (Roman)
    assert is_urgent_message("lagach call kar khup rakt yet ahe", patterns)
    assert is_urgent_message("apghat jhala ahe tavadtob phone kar", patterns)
    assert is_urgent_message("shwas gheta yet nahi ambulance bolva", patterns)
    assert is_urgent_message("chhatit dukhatahe madat havi ahe lagach", patterns)

    # Marathi (Devanagari)
    assert is_urgent_message("श्वास घेता येत नाही रुग्णवाहिका बोलवा", patterns)
    assert is_urgent_message("छातीत दुखत आहे तातडीने फोन कर", patterns)
    assert is_urgent_message("अपघात झाला आहे लगेच कॉल कर", patterns)
    assert is_urgent_message("धोक्यात आहे वाचवा", patterns)


def test_benign_messages_do_not_match() -> None:
    """Assert normal, casual, and business messages never trigger false positives."""
    patterns = load_urgent_phrases()

    benign_samples = [
        "Hey, are you free for coffee tomorrow?",
        "Can we catch up on the quarterly report this Friday?",
        "Did you finish reviewing the pull request?",
        "The food was delicious, thanks for dinner!",
        "Happy birthday! Wishing you an amazing year ahead.",
        "Kal sham ko movie dekhne chalte hain.",
        "Aapka kaam ho gaya kya?",
        "Udyach plan kay ahe? Jevayla jaycha ka?",
        "उद्या दुपारी तीन वाजता भेटूया का?",
        "आजचे जेवण खूप छान होते धन्यवाद.",
        "The meeting was pushed back by thirty minutes.",
    ]
    for msg in benign_samples:
        assert not is_urgent_message(msg, patterns), f"False positive on benign message: {msg}"


# -----------------------------------------------------------------------------
# 2. End-to-End Gateway Triage, Rate-Limiting & Memory Storage
# -----------------------------------------------------------------------------

def test_gateway_urgent_message_handling_end_to_end(
    tmp_path: Any, monkeypatch: Any, caplog: Any
) -> None:
    """Verify urgent message handling:
    1. Stores in contact memory.
    2. Sends owner notice formatted with label and last2 digits.
    3. No draft row in DB.
    4. Model draft provider is never called.
    5. Rate limiting: 1 notice per 2 mins per contact.
    6. Canary token absent from logs and audit records.
    """
    from src.config.models import WhatsAppConfig
    from src.gateway.store import GatewayStore
    from src.memory.contact_memory import EncryptedContactMemoryStore
    from src.security.contact_key_store import ContactKeyStore
    from src.storage.database import initialize_runtime_database

    caplog.set_level(logging.DEBUG)

    db_path = tmp_path / "test_aegis_urgent.db"
    conn = initialize_runtime_database(str(db_path))

    # Seed contact settings
    contact_jid = "919876543210@s.whatsapp.net"
    owner_jid = "919999999999@s.whatsapp.net"
    conn.execute(
        "INSERT INTO whatsapp_contact_settings (contact_jid, label, style_mode, updated_at) VALUES (?, ?, ?, ?)",
        (contact_jid, "Dr. Banner", "normal", "2026-11-26T12:00:00Z"),
    )
    conn.commit()

    key_store = ContactKeyStore(conn)
    memory_store = EncryptedContactMemoryStore(conn, key_store)
    audit_writer = CapturingAuditWriter()
    failing_provider = FailingDraftProvider()
    draft_mgr = DraftManager(
        conn=conn,
        key_store=key_store,
        draft_provider=failing_provider,
        audit_writer=audit_writer,
    )
    gateway_store = GatewayStore(conn)
    transport = SimulatedWhatsAppTransport()
    supervisor = CapturingSupervisor()

    config = WhatsAppConfig(
        enabled=True,
        owner_jid=owner_jid,
        allowed_jids=[contact_jid],
        max_message_length=4000,
    )

    gateway = ChannelGateway(
        config=config,
        store=gateway_store,
        transport=transport,
        router=MagicMock(),
        draft_manager=draft_mgr,
        contact_memory_store=memory_store,
        supervisor=supervisor,
        audit_writer=audit_writer,
    )

    canary = "CANARY_TOKEN_EMERGENCY_CRISIS_777"
    urgent_text = f"I am having severe chest pain, please call an ambulance! {canary}"

    # Inbound urgent message 1
    transport.enqueue_synthetic_message(
        sender_jid=contact_jid,
        text=urgent_text,
        message_id="msg_urgent_1",
    )

    results_1 = gateway.poll_and_process_once()
    assert len(results_1) == 1
    res1 = results_1[0]
    assert res1.state == UpdateLifecycleState.COMPLETED
    assert res1.error_summary == "urgent_message_no_draft"

    # 1. Check supervisor received exactly 1 formatted owner notice
    assert len(supervisor.notices) == 1
    notice = supervisor.notices[0]
    assert "[AEGIS] URGENT: Dr. Banner (...10) may need you. No draft prepared. Please read their message yourself and reply directly." in notice

    # 2. Check memory store contains the message
    memories = memory_store.list_contact_memories(contact_jid)
    assert len(memories) >= 1
    assert any(canary in m["data"].get("text", "") for m in memories)

    # 3. Check NO draft row created
    cur = conn.execute("SELECT COUNT(*) FROM whatsapp_drafts")
    draft_count = cur.fetchone()[0]
    assert draft_count == 0, "No draft row should be created for urgent messages"

    # 4. Check audit event recorded
    urgent_audits = [e for e in audit_writer.events if e["policy_rule_id"] == "urgent_message_no_draft"]
    assert len(urgent_audits) == 1
    assert urgent_audits[0]["telemetry"]["rate_limited"] is False
    assert urgent_audits[0]["telemetry"]["contact"] == "***10"

    # 5. Inbound urgent message 2 within 2 minutes (rate limited notice)
    transport.enqueue_synthetic_message(
        sender_jid=contact_jid,
        text="Cannot breathe, please hurry!",
        message_id="msg_urgent_2",
    )

    results_2 = gateway.poll_and_process_once()
    assert len(results_2) == 1
    res2 = results_2[0]
    assert res2.state == UpdateLifecycleState.COMPLETED

    # Notice count should NOT increase (rate-limited)
    assert len(supervisor.notices) == 1

    # But audit should record rate_limited: True
    urgent_audits_2 = [e for e in audit_writer.events if e["policy_rule_id"] == "urgent_message_no_draft"]
    assert len(urgent_audits_2) == 2
    assert urgent_audits_2[1]["telemetry"]["rate_limited"] is True

    # Memory store should still have stored message 2
    memories2 = memory_store.list_contact_memories(contact_jid)
    assert len(memories2) == 2

    # 6. Verify rate limit expiration (> 120s)
    # Advance timestamp artificially
    gateway._last_urgent_notice_by_contact[contact_jid] -= 125.0

    transport.enqueue_synthetic_message(
        sender_jid=contact_jid,
        text="Help me urgently!",
        message_id="msg_urgent_3",
    )
    results_3 = gateway.poll_and_process_once()
    assert len(results_3) == 1
    assert len(supervisor.notices) == 2

    # 7. Canary check: Ensure the secret canary string is NOT in any log record or audit event telemetry/target/actor
    for rec in caplog.records:
        assert canary not in rec.getMessage(), f"Canary found in log message: {rec.getMessage()}"

    for ev in audit_writer.events:
        assert canary not in ev["policy_rule_id"]
        assert canary not in ev["actor"]
        assert canary not in ev["target_entity_id"]
        for val in ev["telemetry"].values():
            assert canary not in str(val), f"Canary leaked in audit telemetry: {val}"
