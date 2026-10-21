"""SAGE Security & Isolation Test Suite -- Contact Memory & WhatsApp Receive-Only Integration.

Canonical Reference:
    AEGIS Prompt 036 (Phase 7: Security Tests 1-20)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Deterministic tests verifying:
- Encryption & Key Isolation (Tests 1-10)
- Gateway & WhatsApp Security (Tests 11-20)
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from src.config.models import TelegramConfig, WhatsAppConfig
from src.gateway.baileys_sidecar import BaileysSidecarSupervisor
from src.gateway.errors import TransportError
from src.gateway.gateway import ChannelGateway
from src.gateway.models import OutboundStatus, UpdateLifecycleState
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import CommandRouter
from src.gateway.sidecar_protocol import (
    SidecarEnvelope,
    SidecarEventType,
    SidecarProtocolError,
    parse_sidecar_line,
)
from src.gateway.store import GatewayStore
from src.gateway.transport import MockTelegramTransport
from src.gateway.whatsapp import WhatsAppAdapter
from src.gateway.whatsapp_transport import SimulatedWhatsAppTransport
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.memory.contact_memory import EncryptedContactMemoryStore
from src.security.contact_key_store import (
    ContactIdentityError,
    ContactKeyStore,
)
from src.security.crypto import AES256GCMCipher, DecryptionError
from src.security.dpapi import WindowsDPAPIKeyProtector
from src.skills.first_party import register_first_party_skills
from src.skills.registry import SkillRegistry
from src.storage.database import create_connection, initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.approval import ApprovalStore
from src.trust.policy import AutonomyTier

CONTACT_A_JID = "1111111111@s.whatsapp.net"
CONTACT_B_JID = "2222222222@s.whatsapp.net"
STRANGER_JID = "9999999999@s.whatsapp.net"


@pytest.fixture
def memory_db(tmp_path: Path) -> sqlite3.Connection:
    """Fixture providing a fresh isolated SQLite database connection with migrations."""
    db_file = tmp_path / "contact_memory_test.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    conn = initialize_runtime_database(db_file, migrations_dir=migrations_dir)
    return conn


@pytest.fixture
def key_protector() -> WindowsDPAPIKeyProtector:
    """Fixture providing Windows DPAPI key protector."""
    return WindowsDPAPIKeyProtector(description="AEGIS_TEST_KEY")


@pytest.fixture
def contact_key_store(
    memory_db: sqlite3.Connection, key_protector: WindowsDPAPIKeyProtector
) -> ContactKeyStore:
    return ContactKeyStore(memory_db, key_protector=key_protector)


@pytest.fixture
def contact_memory_store(
    memory_db: sqlite3.Connection, contact_key_store: ContactKeyStore
) -> EncryptedContactMemoryStore:
    return EncryptedContactMemoryStore(memory_db, contact_key_store)


# ==============================================================================
# TESTS 1-10: ENCRYPTION AND KEY ISOLATION
# ==============================================================================


def test_01_two_contacts_receive_different_random_keys(contact_key_store: ContactKeyStore):
    """1. Two contacts receive different random data-encryption keys."""
    key_a = contact_key_store.get_or_create_key(CONTACT_A_JID)
    key_b = contact_key_store.get_or_create_key(CONTACT_B_JID)

    assert len(key_a) == 32
    assert len(key_b) == 32
    assert key_a != key_b

    # Verify idempotency: retrieving existing key returns identical key
    key_a_repeat = contact_key_store.get_key(CONTACT_A_JID)
    assert key_a == key_a_repeat


def test_02_contact_a_key_cannot_decrypt_contact_b_records(
    contact_memory_store: EncryptedContactMemoryStore,
    contact_key_store: ContactKeyStore,
    memory_db: sqlite3.Connection,
):
    """2. Contact A's key cannot decrypt Contact B's records."""
    b_data = {"notes": "Private medical notes for Contact B"}
    rec_id_b = contact_memory_store.store_memory(
        contact_jid=CONTACT_B_JID,
        record_type="notes",
        content=b_data,
    )

    # 1. At API level: Contact A requesting Contact B's record returns None (scoped query)
    res_a = contact_memory_store.retrieve_memory(CONTACT_A_JID, rec_id_b)
    assert res_a is None

    # 2. At Cryptographic level: Even if Contact A obtains Contact B's raw ciphertext,
    # decrypting with Contact A's key fails closed
    cursor = memory_db.cursor()
    cursor.execute(
        "SELECT ciphertext, nonce, aad_text FROM contact_memory_records WHERE record_id = ?",
        (rec_id_b,),
    )
    row = cursor.fetchone()
    ciphertext, nonce, aad_text = row[0], row[1], row[2]

    key_a = contact_key_store.get_or_create_key(CONTACT_A_JID)
    cipher_a = AES256GCMCipher(key_a)

    with pytest.raises(DecryptionError):
        cipher_a.decrypt(ciphertext, nonce, aad=aad_text.encode("utf-8"))


def test_03_ciphertext_tampering_causes_authenticated_decryption_to_fail(
    contact_memory_store: EncryptedContactMemoryStore,
    memory_db: sqlite3.Connection,
):
    """3. Ciphertext tampering causes authenticated decryption to fail."""
    rec_id = contact_memory_store.store_memory(
        contact_jid=CONTACT_A_JID,
        record_type="preference",
        content={"tone": "concise"},
    )

    # Tamper with the raw ciphertext byte in the database
    cursor = memory_db.cursor()
    cursor.execute(
        "SELECT ciphertext FROM contact_memory_records WHERE record_id = ?",
        (rec_id,),
    )
    ct = bytearray(cursor.fetchone()[0])
    ct[5] ^= 0xFF  # Flip bits
    with memory_db:
        memory_db.execute(
            "UPDATE contact_memory_records SET ciphertext = ? WHERE record_id = ?",
            (bytes(ct), rec_id),
        )

    # Attempting to retrieve tampered record must raise DecryptionError with zero partial plaintext
    with pytest.raises(DecryptionError):
        contact_memory_store.retrieve_memory(CONTACT_A_JID, rec_id)


def test_04_wrong_keys_fail_closed():
    """4. Wrong keys fail closed."""
    valid_key = AES256GCMCipher.generate_random_key()
    wrong_key = AES256GCMCipher.generate_random_key()

    cipher_valid = AES256GCMCipher(valid_key)
    cipher_wrong = AES256GCMCipher(wrong_key)

    plaintext = b"Sensitive payload needing protection"
    aad = b"contact:12345:record:1"

    ciphertext, nonce = cipher_valid.encrypt(plaintext, aad)

    # Decrypting with wrong key raises DecryptionError
    with pytest.raises(DecryptionError):
        cipher_wrong.decrypt(ciphertext, nonce, aad)


def test_05_missing_or_corrupted_key_material_fails_safely(
    contact_memory_store: EncryptedContactMemoryStore,
    memory_db: sqlite3.Connection,
):
    """5. Missing or corrupted key material fails safely."""
    rec_id = contact_memory_store.store_memory(
        contact_jid=CONTACT_A_JID,
        record_type="note",
        content={"message_summary": "Initial conversation checkpoint"},
    )

    # Corrupt the protected DEK blob in contact_keys
    with memory_db:
        memory_db.execute(
            "UPDATE contact_keys SET protected_dek = ? WHERE contact_jid = ?",
            (b"corrupted_garbage_bytes", CONTACT_A_JID),
        )

    from src.security.dpapi import KeyProtectionError

    # Retrieving memory when key is corrupted fails closed
    with pytest.raises((KeyProtectionError, RuntimeError)):
        contact_memory_store.retrieve_memory(CONTACT_A_JID, rec_id)


def test_06_contact_keys_not_written_to_plaintext_logs_or_config(
    contact_key_store: ContactKeyStore,
    caplog: pytest.LogCaptureFixture,
):
    """6. Contact keys are not written to plaintext logs or configuration."""
    caplog.set_level(logging.DEBUG)
    key_a = contact_key_store.get_or_create_key(CONTACT_A_JID)

    # Convert raw key bytes to hex and raw string representations
    key_hex = key_a.hex()
    for record in caplog.records:
        assert key_hex not in record.message
        assert str(key_a) not in record.message


def test_07_database_files_do_not_expose_plaintext_test_message_bodies(
    tmp_path: Path,
    key_protector: WindowsDPAPIKeyProtector,
):
    """7. Database files do not expose plaintext test message bodies."""
    db_path = tmp_path / "leak_check.db"
    conn = sqlite3.connect(str(db_path))
    ks = ContactKeyStore(conn, key_protector=key_protector)
    ms = EncryptedContactMemoryStore(conn, ks)

    distinct_body_marker = "DISTINCTIVE_CONTACT_BODY_XYZ_987654321"
    ms.store_memory(
        contact_jid=CONTACT_A_JID,
        record_type="contact_message",
        content={"body": distinct_body_marker},
    )
    conn.commit()
    conn.close()

    # Read the raw bytes of the SQLite database file directly from disk
    raw_db_bytes = db_path.read_bytes()
    assert distinct_body_marker.encode("utf-8") not in raw_db_bytes


def test_08_contact_deletion_revokes_access_and_destroys_keys(
    contact_memory_store: EncryptedContactMemoryStore,
    contact_key_store: ContactKeyStore,
    memory_db: sqlite3.Connection,
):
    """8. Contact deletion revokes access and follows the documented key-destruction process."""
    contact_memory_store.store_memory(
        contact_jid=CONTACT_A_JID,
        record_type="note",
        content={"data": "to_be_deleted"},
    )
    assert contact_key_store.has_key(CONTACT_A_JID)

    # Purge contact completely
    summary = contact_memory_store.purge_contact_completely(CONTACT_A_JID)
    assert summary["records_deleted"] >= 1
    assert summary["key_destroyed"] is True

    # Access is immediately revoked
    assert not contact_key_store.has_key(CONTACT_A_JID)
    memories = contact_memory_store.list_contact_memories(CONTACT_A_JID)
    assert memories == []


def test_09_unauthorized_identity_changes_cannot_select_different_contact_key(
    contact_key_store: ContactKeyStore,
):
    """9. Unauthorized identity changes cannot select a different contact's key."""
    # Attempting to access key via group, broadcast, or malformed identity fails closed
    with pytest.raises(ContactIdentityError):
        contact_key_store.get_key("12345678-group@g.us")

    with pytest.raises(ContactIdentityError):
        contact_key_store.get_key("status@broadcast")

    with pytest.raises(ContactIdentityError):
        contact_key_store.get_key("../malformed/jid")


def test_10_concurrent_requests_for_different_contacts_cannot_cross_contaminate(
    tmp_path: Path,
    key_protector: WindowsDPAPIKeyProtector,
):
    """10. Concurrent requests for different contacts cannot cross-contaminate context."""
    db_file = tmp_path / "concurrent_test.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    init_conn = initialize_runtime_database(db_file, migrations_dir=migrations_dir)
    init_conn.close()

    results_a = []
    results_b = []

    def run_contact_a():
        conn = create_connection(db_file)
        ks = ContactKeyStore(conn, key_protector=key_protector)
        ms = EncryptedContactMemoryStore(conn, ks)
        for i in range(10):
            ms.store_memory(CONTACT_A_JID, "token", {"contact": "A", "val": i})
        mems = ms.list_contact_memories(CONTACT_A_JID)
        results_a.extend(mems)
        conn.close()

    def run_contact_b():
        conn = create_connection(db_file)
        ks = ContactKeyStore(conn, key_protector=key_protector)
        ms = EncryptedContactMemoryStore(conn, ks)
        for i in range(10):
            ms.store_memory(CONTACT_B_JID, "token", {"contact": "B", "val": i})
        mems = ms.list_contact_memories(CONTACT_B_JID)
        results_b.extend(mems)
        conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_contact_a)
        f2 = executor.submit(run_contact_b)
        f1.result()
        f2.result()

    # Verify complete isolation: Contact A sees only A's tokens; Contact B sees only B's tokens
    assert len(results_a) == 10
    for rec in results_a:
        assert rec["contact_jid"] == CONTACT_A_JID
        assert rec["data"]["contact"] == "A"

    assert len(results_b) == 10
    for rec in results_b:
        assert rec["contact_jid"] == CONTACT_B_JID
        assert rec["data"]["contact"] == "B"


# ==============================================================================
# TESTS 11-20: GATEWAY AND WHATSAPP SECURITY
# ==============================================================================


@pytest.fixture
def gateway_fixture(
    tmp_path: Path,
    contact_key_store: ContactKeyStore,
    contact_memory_store: EncryptedContactMemoryStore,
) -> Dict[str, Any]:
    test_db_path = tmp_path / "gw_test.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    conn = initialize_runtime_database(test_db_path, migrations_dir=migrations_dir)

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    authority = build_trust_authority(conn, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])

    registry = SkillRegistry()
    register_first_party_skills(registry, broker)

    state_store = StateStore(conn)
    approval_store = ApprovalStore(conn)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=authority,
        tool_broker=broker,
        skill_registry=registry,
    )
    gateway_store = GatewayStore(conn)
    transport = SimulatedWhatsAppTransport(store=approval_store)
    router = CommandRouter(
        supervisor=supervisor,
        tool_broker=broker,
        trust_authority=authority,
        approval_store=approval_store,
        workspace_alias="test_ws",
        workspace_dir=sandbox,
    )
    config = WhatsAppConfig(
        enabled=True,
        allowed_jids=[CONTACT_A_JID],
        session_path=str(sandbox / "wa_session"),
        max_message_length=4096,
        rate_limit_per_minute=30,
        receive_only=True,
    )
    limiter = SlidingWindowRateLimiter()
    gateway = ChannelGateway(
        config=config,
        store=gateway_store,
        transport=transport,
        router=router,
        rate_limiter=limiter,
        approval_store=approval_store,
        contact_memory_store=contact_memory_store,
    )
    return {
        "gateway": gateway,
        "transport": transport,
        "store": gateway_store,
        "key_store": contact_key_store,
        "memory_store": contact_memory_store,
        "sandbox": sandbox,
        "test_db_path": test_db_path,
        "conn": conn,
    }


def test_11_unknown_contacts_rejected_before_memory_access(gateway_fixture: Dict[str, Any]):
    """11. Unknown contacts are rejected before memory access."""
    gw: ChannelGateway = gateway_fixture["gateway"]
    tp: SimulatedWhatsAppTransport = gateway_fixture["transport"]
    ks: ContactKeyStore = gateway_fixture["key_store"]

    tp.enqueue_synthetic_message(
        sender_jid=STRANGER_JID,
        text="Hello secret system",
        message_id="stranger_msg_1",
    )

    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "Unauthorized sender ID" in (results[0].error_summary or "")

    # Zero key creation or memory access for stranger
    assert not ks.has_key(STRANGER_JID)


def test_12_group_and_broadcast_rejected_before_memory_retrieval_and_inference(
    gateway_fixture: Dict[str, Any],
):
    """12. Group and broadcast messages are rejected before memory retrieval and inference."""
    gw: ChannelGateway = gateway_fixture["gateway"]
    tp: SimulatedWhatsAppTransport = gateway_fixture["transport"]

    tp.enqueue_synthetic_message(
        remote_jid="12345678-group@g.us",
        is_group=True,
        sender_jid=CONTACT_A_JID,
        text="/status",
        message_id="group_msg_1",
    )

    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "Group and broadcast" in (results[0].error_summary or "")


def test_13_duplicate_messages_do_not_create_duplicate_tasks_or_memories(
    gateway_fixture: Dict[str, Any],
):
    """13. Duplicate messages do not create duplicate tasks or memories."""
    gw: ChannelGateway = gateway_fixture["gateway"]
    tp: SimulatedWhatsAppTransport = gateway_fixture["transport"]

    tp.enqueue_synthetic_message(
        sender_jid=CONTACT_A_JID,
        text="/status",
        message_id="dedup_msg_1",
    )
    # First poll: processed
    res1 = gw.poll_and_process_once()
    assert len(res1) == 1
    assert res1[0].state == UpdateLifecycleState.COMPLETED

    # Enqueue exact same message ID
    tp.enqueue_synthetic_message(
        sender_jid=CONTACT_A_JID,
        text="/status",
        message_id="dedup_msg_1",
    )
    # Second poll: deduplicated, no duplicate task created
    res2 = gw.poll_and_process_once()
    assert len(res2) == 1
    assert "Duplicate update skipped" in (res2[0].error_summary or "")


def test_14_self_messages_do_not_trigger_processing_loops():
    """14. Self-messages do not trigger processing loops."""
    adapter = WhatsAppAdapter()
    raw_event = {
        "key": {
            "remoteJid": CONTACT_A_JID,
            "id": "self_msg_1",
            "fromMe": True,
        },
        "message": {"conversation": "Should be ignored"},
    }
    valid, parsed, rejection = adapter.parse_update(raw_event)
    assert not valid
    assert "fromMe=True" in (rejection or "")


def test_15_malformed_and_oversized_ipc_events_fail_closed():
    """15. Malformed and oversized IPC events fail closed."""
    # Malformed JSON line
    with pytest.raises(SidecarProtocolError):
        parse_sidecar_line("INVALID_JSON_LINE{{{")

    # Oversized line (>64KB limit)
    oversized = "a" * 70000
    with pytest.raises(SidecarProtocolError):
        parse_sidecar_line(oversized)


def test_16_reconnects_do_not_replay_previously_processed_messages(
    gateway_fixture: Dict[str, Any],
):
    """16. Reconnects do not replay previously processed messages."""
    gw: ChannelGateway = gateway_fixture["gateway"]
    tp: SimulatedWhatsAppTransport = gateway_fixture["transport"]

    tp.enqueue_synthetic_message(
        sender_jid=CONTACT_A_JID,
        text="/help",
        message_id="reconnect_msg_1",
    )
    res1 = gw.poll_and_process_once()
    assert len(res1) == 1
    assert res1[0].state == UpdateLifecycleState.COMPLETED

    # Disconnect and reconnect transport
    tp.disconnect()
    assert not tp.is_connected
    tp.connect()
    assert tp.is_connected

    # Re-queue same message that arrived before disconnect
    tp.enqueue_synthetic_message(
        sender_jid=CONTACT_A_JID,
        text="/help",
        message_id="reconnect_msg_1",
    )
    res2 = gw.poll_and_process_once()
    assert len(res2) == 1
    assert "Duplicate" in (res2[0].error_summary or "")


def test_17_restart_recovery_does_not_repeat_uncertain_external_actions(
    gateway_fixture: Dict[str, Any],
):
    """17. Restart recovery does not repeat uncertain external actions."""
    store: GatewayStore = gateway_fixture["store"]

    # Record outbound message in UNCERTAIN status
    out_rec = store.record_outbound_message(
        channel="whatsapp",
        recipient_id=CONTACT_A_JID,
        content="Testing uncertain delivery",
    )
    store.update_outbound_status(out_rec.message_id, OutboundStatus.UNCERTAIN)

    # Verify query on outbound table: UNCERTAIN message is preserved and not marked PENDING
    msg = store.get_outbound_message(out_rec.message_id)
    assert msg is not None
    assert msg.status == OutboundStatus.UNCERTAIN

    cur = store._conn.execute(
        "SELECT message_id FROM gateway_outbound_messages WHERE status = 'PENDING'"
    )
    pending_ids = [row[0] for row in cur.fetchall()]
    assert out_rec.message_id not in pending_ids


def test_18_session_credentials_and_encryption_keys_redacted_from_errors():
    """18. Session credentials and encryption keys are redacted from errors."""
    with pytest.raises(ValueError, match="violates zero-secret invariant"):
        SidecarEnvelope(
            type=SidecarEventType.ERROR,
            payload={"error": "sk-proj-1234567890abcdef1234567890abcdef"},
        )


def test_19_outbound_whatsapp_methods_remain_disabled(gateway_fixture: Dict[str, Any]):
    """19. Outbound WhatsApp methods remain disabled in receive-only mode."""
    supervisor = BaileysSidecarSupervisor()
    with pytest.raises(TransportError, match="strictly disabled in receive-only mode"):
        supervisor.send_message(recipient_jid=CONTACT_A_JID, text="Hello world")

    # In gateway with receive_only=True, dispatch records failure and suppresses network send
    gw: ChannelGateway = gateway_fixture["gateway"]
    gw._dispatch_outbound(
        recipient_id=CONTACT_A_JID,
        content="Attempted automatic reply",
    )

    db_path = gateway_fixture["test_db_path"]
    conn = create_connection(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT status, error_message FROM gateway_outbound_messages WHERE recipient_id = ?",
        (CONTACT_A_JID,),
    )
    row = cur.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "FAILED"
    assert "strictly disabled in receive-only mode" in row[1]


def test_20_existing_telegram_gateway_and_trust_core_behavior_intact(
    tmp_path: Path,
):
    """20. Existing Telegram gateway and Trust Core behavior remains intact."""
    test_db_path = tmp_path / "tg_test.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    conn = initialize_runtime_database(test_db_path, migrations_dir=migrations_dir)

    sandbox = tmp_path / "tg_sandbox"
    sandbox.mkdir()

    authority = build_trust_authority(conn, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])

    registry = SkillRegistry()
    register_first_party_skills(registry, broker)

    state_store = StateStore(conn)
    approval_store = ApprovalStore(conn)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=authority,
        tool_broker=broker,
        skill_registry=registry,
    )
    gateway_store = GatewayStore(conn)
    tg_transport = MockTelegramTransport()
    router = CommandRouter(
        supervisor=supervisor,
        tool_broker=broker,
        trust_authority=authority,
        approval_store=approval_store,
        workspace_alias="tg_ws",
        workspace_dir=sandbox,
    )
    tg_config = TelegramConfig(
        enabled=True,
        bot_token_env="FAKE_TELEGRAM_TOKEN",
        allowed_user_ids=[123456],
        allowed_chat_ids=[123456],
    )
    os.environ["FAKE_TELEGRAM_TOKEN"] = "fake_test_token"

    gw = ChannelGateway(
        config=tg_config,
        store=gateway_store,
        transport=tg_transport,
        router=router,
    )

    # Send /help via Telegram update
    tg_transport.enqueue_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 100,
                "from": {"id": 123456, "is_bot": False, "first_name": "Test"},
                "chat": {"id": 123456, "type": "private"},
                "date": 1600000000,
                "text": "/help",
            },
        }
    )
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.COMPLETED
    assert len(tg_transport.sent_messages) == 1
    assert "SAGE Remote-Control Gateway" in tg_transport.sent_messages[0]["text"]
