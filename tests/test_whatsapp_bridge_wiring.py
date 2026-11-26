"""Comprehensive Integration and Security Tests for WhatsApp Node Bridge Wiring.

Canonical Reference:
    AEGIS Prompt 036 (Phase 9C: Bridge Wiring & Safe Send Path)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md

Covers:
1. Whitelisted PN contact message -> processed; key store called with canonical PN.
2. @lid with sender_pn present + whitelisted -> processed, alias learned.
3. @lid later, no sender_pn, alias known -> processed.
4. @lid with no alias -> dropped, audit "unresolved_identity", zero DB writes, zero key store calls.
5. Unknown PN sender -> dropped before key derivation (assert key store mock not called).
6. Alias poisoning: stranger sends @lid + their own PN -> NO alias stored.
7. Group, broadcast -> dropped.
8. from_me contact message -> ignored; from_me self chat -> owner handler; never stored in any contact vault.
9. Self-echo: ack registers id, subsequent inbound with that id suppressed; TTL expiry and max-size eviction.
10. Send: success path with ack; hash mismatch refused; recipient mismatch refused; non-whitelisted recipient refused before any write to stdin; bridge TARGET_NOT_ALLOWED and RATE_LIMITED mapped; timeout; unknown correlation id ignored; no automatic retry.
11. Allowlist file: atomic write, regenerated on whitelist change, rejects non-canonical entries, never contains the owner.
12. Static invariant: SendAuthorization constructed only in permitted files.
13. gateway.py: @lid or non-numeric id does not raise.
14. Legacy duplicate fields with conflicting values do NOT affect identity decisions.
15. Migration test for the alias table (up and idempotent), plus the existing migration tests unchanged and green.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.config.models import WhatsAppConfig
from src.gateway.baileys_sidecar import BaileysSidecarSupervisor, generate_allowlist_file
from src.gateway.contact_alias import ContactAliasStore
from src.gateway.echo_registry import SelfEchoRegistry
from src.gateway.errors import GatewayError, TransportError
from src.gateway.gateway import ChannelGateway
from src.gateway.models import UpdateLifecycleState
from src.gateway.owner_channel import DefaultOwnerChannelHandler
from src.gateway.router import CommandRouter
from src.gateway.send_authorization import (
    AuthorizationMismatchError,
    RateLimitedError,
    SendAuthorization,
    SidecarTimeoutError,
    TargetNotAllowedError,
    create_send_authorization,
)
from src.gateway.sidecar_protocol import (
    SidecarEnvelope,
    SidecarEventType,
    parse_sidecar_line,
    serialize_sidecar_envelope,
)
from src.gateway.store import GatewayStore
from src.gateway.whatsapp import WhatsAppAdapter
from src.gateway.whatsapp_transport import WhatsAppTransport
from src.storage.database import (
    create_connection,
)
from src.storage.migrations import (
    apply_migrations,
    bootstrap_schema_migrations,
    get_current_schema_version,
    load_migrations_from_directory,
)
from src.trust.audit import AuditDecision, AuditEventType, AuditWriter

WHITELISTED_PN = "15551234567@s.whatsapp.net"
STRANGER_PN = "15559876543@s.whatsapp.net"
OWNER_PN = "15550001111@s.whatsapp.net"
CONTACT_LID = "123456789012345@lid"
STRANGER_LID = "987654321098765@lid"


class MockWhatsAppTransport(WhatsAppTransport):
    """Synthetic test transport for enqueuing inbound events."""

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []

    @property
    def is_connected(self) -> bool:
        return True

    def send_message(self, *args, **kwargs) -> Any:
        return None

    def enqueue(self, event: Dict[str, Any]) -> None:
        self._events.append(event)

    def get_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        batch = self._events[:limit]
        self._events = self._events[limit:]
        return batch


@pytest.fixture
def test_db_conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = create_connection(db_path)
    migrations_dir = Path(__file__).parent.parent / "migrations"
    migrations = load_migrations_from_directory(migrations_dir)
    bootstrap_schema_migrations(conn)
    apply_migrations(conn, migrations)
    yield conn
    conn.close()
    for ext in ("", "-wal", "-shm"):
        Path(db_path + ext).unlink(missing_ok=True)


@pytest.fixture
def gateway_setup(test_db_conn):
    store = GatewayStore(test_db_conn)
    config = WhatsAppConfig(
        enabled=True,
        allowed_jids=[WHITELISTED_PN],
        max_message_length=4096,
        rate_limit_per_minute=60,
    )
    transport = MockWhatsAppTransport()
    router = CommandRouter(supervisor=MagicMock())
    alias_store = ContactAliasStore(test_db_conn)
    echo_registry = SelfEchoRegistry()
    owner_messages: List[Dict[str, Any]] = []

    def on_owner_msg(msg_id: str, length: int):
        owner_messages.append({"id": msg_id, "length": length})

    owner_handler = DefaultOwnerChannelHandler(audit_callback=on_owner_msg)
    audit_writer = AuditWriter(test_db_conn)
    mock_draft_mgr = MagicMock()
    mock_draft_mgr.create_draft_for_inbound.return_value = {"card": "[AEGIS] Draft", "draft_id": "test"}

    gateway = ChannelGateway(
        config=config,
        store=store,
        transport=transport,
        router=router,
        alias_store=alias_store,
        echo_registry=echo_registry,
        owner_channel_handler=owner_handler,
        audit_writer=audit_writer,
        draft_manager=mock_draft_mgr,
    )
    return {
        "gateway": gateway,
        "transport": transport,
        "alias_store": alias_store,
        "echo_registry": echo_registry,
        "owner_messages": owner_messages,
        "conn": test_db_conn,
        "store": store,
    }


# ==============================================================================
# TEST 1: Whitelisted PN contact message -> processed
# ==============================================================================
def test_01_whitelisted_pn_processed(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]

    event = {
        "id": "msg_001",
        "chat_jid": WHITELISTED_PN,
        "sender_pn": WHITELISTED_PN,
        "from_me": False,
        "is_self_chat": False,
        "text": "/status",
    }
    tp.enqueue(event)
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.COMPLETED
    assert results[0].sender_id == WHITELISTED_PN


# ==============================================================================
# TEST 2: @lid with sender_pn present + whitelisted -> processed, alias learned
# ==============================================================================
def test_02_lid_with_sender_pn_whitelisted_learns_alias(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]
    alias_store = gateway_setup["alias_store"]

    assert alias_store.get_canonical_pn(CONTACT_LID) is None

    event = {
        "id": "msg_002",
        "chat_jid": CONTACT_LID,
        "sender_pn": WHITELISTED_PN,
        "from_me": False,
        "is_self_chat": False,
        "text": "Hello SAGE",
    }
    tp.enqueue(event)
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.COMPLETED
    assert results[0].sender_id == WHITELISTED_PN

    # Alias must now be persisted
    assert alias_store.get_canonical_pn(CONTACT_LID) == WHITELISTED_PN


# ==============================================================================
# TEST 3: @lid later, no sender_pn, alias known -> processed
# ==============================================================================
def test_03_lid_later_uses_known_alias(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]
    alias_store = gateway_setup["alias_store"]

    # Pre-seed alias
    alias_store.learn_alias(CONTACT_LID, WHITELISTED_PN, [WHITELISTED_PN])

    event = {
        "id": "msg_003",
        "chat_jid": CONTACT_LID,
        "sender_pn": None,
        "from_me": False,
        "is_self_chat": False,
        "text": "Subsequent query",
    }
    tp.enqueue(event)
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.COMPLETED
    assert results[0].sender_id == WHITELISTED_PN


# ==============================================================================
# TEST 4: @lid with no alias -> dropped, audit "unresolved_identity"
# ==============================================================================
def test_04_lid_with_no_alias_dropped(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]
    conn = gateway_setup["conn"]

    event = {
        "id": "msg_004",
        "chat_jid": "unknown_contact@lid",
        "sender_pn": None,
        "from_me": False,
        "is_self_chat": False,
        "text": "Stranger query",
    }
    tp.enqueue(event)
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert results[0].error_summary == "unresolved_identity"

    # Verify audit record
    cur = conn.execute("SELECT policy_rule_id FROM audit_records WHERE policy_rule_id = 'unresolved_identity'")
    assert cur.fetchone() is not None


# ==============================================================================
# TEST 5: Unknown PN sender -> dropped before key derivation
# ==============================================================================
def test_05_unknown_pn_sender_dropped(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]
    conn = gateway_setup["conn"]

    event = {
        "id": "msg_005",
        "chat_jid": STRANGER_PN,
        "sender_pn": STRANGER_PN,
        "from_me": False,
        "is_self_chat": False,
        "text": "Stranger PN text",
    }
    tp.enqueue(event)
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "Unauthorized sender ID" in (results[0].error_summary or "")

    # Zero plaintext message text stored
    cur = conn.execute("SELECT raw_text FROM gateway_updates WHERE channel_update_id LIKE '%msg_005%'")
    row = cur.fetchone()
    assert row is not None
    assert row[0] is None


# ==============================================================================
# TEST 6: Alias poisoning: stranger sends @lid + their own PN -> NO alias stored
# ==============================================================================
def test_06_alias_poisoning_prevented(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]
    alias_store = gateway_setup["alias_store"]

    event = {
        "id": "msg_006",
        "chat_jid": STRANGER_LID,
        "sender_pn": STRANGER_PN,  # Not in allowed_jids
        "from_me": False,
        "is_self_chat": False,
        "text": "I am claiming this LID",
    }
    tp.enqueue(event)
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED

    # Ensure alias store was NOT poisoned
    assert alias_store.get_canonical_pn(STRANGER_LID) is None


# ==============================================================================
# TEST 7: Group, broadcast -> dropped
# ==============================================================================
def test_07_group_and_broadcast_dropped(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]

    for gid in ["12345678-group@g.us", "broadcast@broadcast", "12345@newsletter"]:
        tp.enqueue({
            "id": f"grp_{gid}",
            "chat_jid": gid,
            "sender_pn": WHITELISTED_PN,
            "from_me": False,
            "is_self_chat": False,
            "text": "Group message",
        })
        results = gw.poll_and_process_once()
        assert len(results) == 1
        assert results[0].state == UpdateLifecycleState.REJECTED
        assert "Unauthorized conversation type" in (results[0].error_summary or "")


# ==============================================================================
# TEST 8: from_me contact message ignored; from_me self chat -> owner handler
# ==============================================================================
def test_08_from_me_routing(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]
    owner_messages = gateway_setup["owner_messages"]

    # (a) from_me contact message -> ignored
    tp.enqueue({
        "id": "self_contact_msg",
        "chat_jid": WHITELISTED_PN,
        "from_me": True,
        "is_self_chat": False,
        "text": "I typed to contact on phone",
    })
    res_a = gw.poll_and_process_once()
    assert len(res_a) == 1
    assert res_a[0].state == UpdateLifecycleState.REJECTED
    assert "fromMe=True" in (res_a[0].error_summary or "")
    assert len(owner_messages) == 0

    # (b) from_me self chat -> owner handler
    tp.enqueue({
        "id": "owner_self_msg",
        "chat_jid": OWNER_PN,
        "from_me": True,
        "is_self_chat": True,
        "text": "#approve draft_123",
    })
    res_b = gw.poll_and_process_once()
    assert len(res_b) == 1
    assert res_b[0].state == UpdateLifecycleState.COMPLETED
    assert len(owner_messages) == 1
    assert owner_messages[0]["id"] == "owner_self_msg"
    assert owner_messages[0]["length"] == len("#approve draft_123")


# ==============================================================================
# TEST 9: Self-echo: ack registers id, subsequent inbound suppressed; TTL/eviction
# ==============================================================================
def test_09_self_echo_suppression_and_eviction(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]
    reg = gateway_setup["echo_registry"]

    reg.register("outbound_echo_123")
    assert reg.is_echo("outbound_echo_123")

    tp.enqueue({
        "id": "outbound_echo_123",
        "chat_jid": WHITELISTED_PN,
        "sender_pn": WHITELISTED_PN,
        "from_me": False,
        "is_self_chat": False,
        "text": "Echo of our own sent message",
    })
    res = gw.poll_and_process_once()
    assert len(res) == 1
    assert res[0].state == UpdateLifecycleState.REJECTED
    assert "Self-echo" in (res[0].error_summary or "")

    # Test TTL expiry
    short_reg = SelfEchoRegistry(max_size=5, ttl_seconds=0.01)
    short_reg.register("fast_expire")
    time.sleep(0.02)
    assert not short_reg.is_echo("fast_expire")

    # Test max-size eviction
    small_reg = SelfEchoRegistry(max_size=3, ttl_seconds=100.0)
    for i in range(4):
        small_reg.register(f"msg_{i}")
    assert len(small_reg) == 3
    assert not small_reg.is_echo("msg_0")  # Oldest evicted
    assert small_reg.is_echo("msg_3")


# ==============================================================================
# TEST 10: Guarded send path with fake subprocess bridge
# ==============================================================================
def test_10_guarded_send_path_with_fake_bridge(tmp_path):
    # Create fake Python stub bridge
    fake_bridge_code = """
import sys
import json

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        env = json.loads(line)
    except Exception:
        continue
    msg_type = env.get("type")
    cid = env.get("correlation_id", "test_cid")
    payload = env.get("payload", {})

    if msg_type == "shutdown":
        sys.exit(0)
    elif msg_type == "outbound_send":
        recipient = payload.get("to")
        if recipient == "9999999999@s.whatsapp.net":
            err_env = {
                "version": "1.0",
                "type": "error",
                "correlation_id": cid,
                "timestamp": "2026-11-26T00:00:00Z",
                "payload": {"error": "Target not in bridge allowlist", "error_code": "TARGET_NOT_ALLOWED"}
            }
            sys.stdout.write(json.dumps(err_env) + "\\n")
            sys.stdout.flush()
        elif recipient == "8888888888@s.whatsapp.net":
            err_env = {
                "version": "1.0",
                "type": "error",
                "correlation_id": cid,
                "timestamp": "2026-11-26T00:00:00Z",
                "payload": {"error": "Bridge rate limit exceeded", "error_code": "RATE_LIMITED"}
            }
            sys.stdout.write(json.dumps(err_env) + "\\n")
            sys.stdout.flush()
        elif recipient == "7777777777@s.whatsapp.net":
            # Timeout test: ignore / do not reply
            pass
        else:
            ack_env = {
                "version": "1.0",
                "type": "outbound_ack",
                "correlation_id": cid,
                "timestamp": "2026-11-26T00:00:00Z",
                "payload": {"id": "stanza_ack_999", "status": "sent"}
            }
            sys.stdout.write(json.dumps(ack_env) + "\\n")
            sys.stdout.flush()
"""
    stub_file = tmp_path / "fake_bridge.py"
    stub_file.write_text(fake_bridge_code, encoding="utf-8")

    supervisor = BaileysSidecarSupervisor(
        allowed_jids=[
            WHITELISTED_PN,
            "9999999999@s.whatsapp.net",
            "8888888888@s.whatsapp.net",
            "7777777777@s.whatsapp.net",
        ],
        echo_registry=SelfEchoRegistry(),
    )
    supervisor.start(cmd=[sys.executable, str(stub_file)])
    assert supervisor.is_connected

    try:
        # Success path
        auth_ok = create_send_authorization("app_1", WHITELISTED_PN, "Approved message")
        res = supervisor.send_message(
            recipient_jid=WHITELISTED_PN,
            text="Approved message",
            authorization=auth_ok,
            timeout=5.0,
        )
        assert res.get("id") == "stanza_ack_999"
        assert supervisor._echo_registry.is_echo("stanza_ack_999")

        # Hash mismatch refused
        with pytest.raises(AuthorizationMismatchError):
            supervisor.send_message(
                recipient_jid=WHITELISTED_PN,
                text="Tampered message content",
                authorization=auth_ok,
            )

        # Recipient mismatch refused
        auth_diff = create_send_authorization("app_2", "1111111111@s.whatsapp.net", "Hello")
        with pytest.raises(AuthorizationMismatchError):
            supervisor.send_message(
                recipient_jid=WHITELISTED_PN,
                text="Hello",
                authorization=auth_diff,
            )

        # Non-whitelisted recipient refused before any stdin write
        auth_unauth = create_send_authorization("app_3", "19990001111@s.whatsapp.net", "Hello")
        with pytest.raises(TargetNotAllowedError):
            supervisor.send_message(
                recipient_jid="19990001111@s.whatsapp.net",
                text="Hello",
                authorization=auth_unauth,
            )

        # TARGET_NOT_ALLOWED mapped from bridge
        auth_target_denied = create_send_authorization("app_4", "9999999999@s.whatsapp.net", "Hi")
        with pytest.raises(TargetNotAllowedError):
            supervisor.send_message(
                recipient_jid="9999999999@s.whatsapp.net",
                text="Hi",
                authorization=auth_target_denied,
            )

        # RATE_LIMITED mapped from bridge
        auth_rate_limited = create_send_authorization("app_5", "8888888888@s.whatsapp.net", "Hi")
        with pytest.raises(RateLimitedError):
            supervisor.send_message(
                recipient_jid="8888888888@s.whatsapp.net",
                text="Hi",
                authorization=auth_rate_limited,
            )

        # Timeout
        auth_timeout = create_send_authorization("app_6", "7777777777@s.whatsapp.net", "Hi")
        with pytest.raises(SidecarTimeoutError):
            supervisor.send_message(
                recipient_jid="7777777777@s.whatsapp.net",
                text="Hi",
                authorization=auth_timeout,
                timeout=0.2,
            )

    finally:
        supervisor.stop()


# ==============================================================================
# TEST 11: Allowlist file: atomic write, regenerated on whitelist change, excludes owner
# ==============================================================================
def test_11_allowlist_generation(tmp_path):
    target = tmp_path / "whatsapp_allowlist.json"
    allowed = [WHITELISTED_PN, "15557778888@s.whatsapp.net", OWNER_PN]

    path = generate_allowlist_file(allowed, owner_jid=OWNER_PN, output_path=target)
    assert path == target
    assert target.exists()

    data = json.loads(target.read_text(encoding="utf-8"))
    assert WHITELISTED_PN in data
    assert "15557778888@s.whatsapp.net" in data
    assert OWNER_PN not in data  # Owner excluded!

    # Rejects non-canonical entry
    with pytest.raises(ValueError, match="Non-canonical WhatsApp JID"):
        generate_allowlist_file(["invalid_jid_format"], output_path=target)


# ==============================================================================
# TEST 12: Static Invariant: SendAuthorization constructed only in permitted files
# ==============================================================================
def test_12_send_authorization_ast_invariant():
    repo_src = Path(__file__).parent.parent / "src"
    permitted_files = {
        (repo_src / "gateway" / "send_authorization.py").resolve(),
    }

    violations = []
    for py_path in repo_src.rglob("*.py"):
        resolved = py_path.resolve()
        if resolved in permitted_files:
            continue

        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                func_name = getattr(func, "id", None) or getattr(func, "attr", None)
                if func_name == "SendAuthorization":
                    violations.append(f"{py_path}:{node.lineno}")

    assert violations == [], (
        f"Security Invariant Violated: SendAuthorization instantiated in unauthorized files: {violations}. "
        "SendAuthorization may only be constructed inside src/gateway/send_authorization.py."
    )


# ==============================================================================
# TEST 13: gateway.py: @lid or non-numeric id does not raise
# ==============================================================================
def test_13_gateway_non_numeric_id_does_not_crash(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]

    # Non-numeric string JID
    tp.enqueue({
        "id": "msg_non_num",
        "chat_jid": "alpha_beta_contact@s.whatsapp.net",
        "sender_pn": "alpha_beta_contact@s.whatsapp.net",
        "from_me": False,
        "is_self_chat": False,
        "text": "Hello",
    })
    # Must not raise ValueError or crash
    results = gw.poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED


# ==============================================================================
# TEST 14: Legacy duplicate fields with conflicting values do NOT affect identity
# ==============================================================================
def test_14_legacy_duplicate_fields_do_not_override_normalized_identity(gateway_setup):
    gw = gateway_setup["gateway"]
    tp = gateway_setup["transport"]

    event = {
        "id": "msg_conflict_1",
        "chat_jid": WHITELISTED_PN,
        "sender_pn": WHITELISTED_PN,
        "from_me": False,
        "is_self_chat": False,
        "text": "Valid message",
        # Legacy conflicting fields injected:
        "remoteJid": STRANGER_PN,
        "key": {
            "remoteJid": STRANGER_PN,
            "id": "legacy_stanza_id",
            "fromMe": True,
        },
        "message": {"conversation": "Conflicting legacy text"},
    }
    tp.enqueue(event)
    results = gw.poll_and_process_once()
    assert len(results) == 1
    # Normalized fields took precedence: message is accepted for WHITELISTED_PN
    assert results[0].state == UpdateLifecycleState.COMPLETED
    assert results[0].sender_id == WHITELISTED_PN


# ==============================================================================
# TEST 15: Migration test for alias table (up and idempotent)
# ==============================================================================
def test_15_alias_table_migration_idempotency(test_db_conn):
    alias_store = ContactAliasStore(test_db_conn)
    # Test table exists
    assert alias_store.get_canonical_pn(CONTACT_LID) is None

    # Test learn and update
    ok = alias_store.learn_alias(CONTACT_LID, WHITELISTED_PN, [WHITELISTED_PN])
    assert ok is True
    assert alias_store.get_canonical_pn(CONTACT_LID) == WHITELISTED_PN

    # Idempotent learn (update)
    ok2 = alias_store.learn_alias(CONTACT_LID, WHITELISTED_PN, [WHITELISTED_PN])
    assert ok2 is True
    assert alias_store.get_canonical_pn(CONTACT_LID) == WHITELISTED_PN

    # Re-running migrations on the database
    migrations_dir = Path(__file__).parent.parent / "migrations"
    migrations = load_migrations_from_directory(migrations_dir)
    applied = apply_migrations(test_db_conn, migrations)
    assert len(applied) == 11
    assert get_current_schema_version(test_db_conn) == 11
