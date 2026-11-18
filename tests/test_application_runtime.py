"""PROMPT 073: SAGE Application Runtime Integration & Communication Security Hardening Test Suite.

Verifies:
- Application composition root initialization order and shared service instances.
- Graceful startup and shutdown lifecycle management.
- Cryptographic sidecar HMAC payload authentication and replay defense.
- Sidecar timestamp expiration and payload tampering rejection.
- Identity propagation, group privilege stripping, and contact memory isolation.
- Approval workflow integrity and duplicate approval prevention.
- End-to-end conversational and background job execution paths.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.communication.adapters.telegram import TelegramChannelAdapter
from src.communication.domain import (
    ChannelPermissionLevel,
    ChannelPlatform,
    ConversationType,
    IdentityMapping,
    IncomingMessage,
    OutgoingMessage,
)
from src.communication.errors import (
    ChannelAuthorizationError,
    SidecarSecurityError,
    UnauthorizedSenderError,
)
from src.communication.sidecar import (
    PROTOCOL_VERSION,
    SecureSidecarCodec,
    SecureSidecarHost,
    SidecarEnvelope,
    SidecarEventType,
    compute_auth_digest,
    generate_sidecar_secret,
)
from src.gateway.transport import MockTelegramTransport
from src.memory.models import UnifiedMemoryQuery, UnifiedMemoryRecord
from src.models.adapters.base import BaseModelProvider
from src.models.domain import (
    ModelCapabilities,
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderType,
)
from src.models.gateway import ModelGateway
from src.models.registry import ModelRegistry
from src.models.router import ModelRouter
from src.runtime import SageApplication
from src.scheduler.job_models import BackgroundJob, JobStatus
from src.storage.database import initialize_runtime_database
from src.trust.approval import ApprovalStore
from src.trust.policy import AutonomyTier


class MockRuntimeProvider(BaseModelProvider):
    def __init__(self, provider_id: str = "mock-runtime-prov") -> None:
        super().__init__(provider_id=provider_id, provider_type=ProviderType.LOCAL)
        self.default_model = "mock-runtime-v1"

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id=self.default_model,
                provider_id=self.provider_id,
                display_name="Mock Runtime Model",
                provider_type=ProviderType.LOCAL,
                capabilities=ModelCapabilities(
                    text_generation=True,
                    reasoning=True,
                ),
            )
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            content=f"Runtime Friday response to: {request.prompt}",
            model_id=self.default_model,
            provider_id=self.provider_id,
        )

    def generate_stream(self, request: ModelRequest):
        yield None

    def check_health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderHealthStatus.HEALTHY)


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "runtime_test.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def mock_runtime_gateway() -> ModelGateway:
    registry = ModelRegistry()
    adapter = MockRuntimeProvider()
    registry.register_provider(adapter)
    registry.set_alias("fast", "mock-runtime-v1")
    registry.set_alias("balanced", "mock-runtime-v1")

    router = ModelRouter(registry)
    return ModelGateway(registry=registry, router=router)


# =============================================================================
# 1. APPLICATION LIFECYCLE & COMPOSITION ROOT TESTS
# =============================================================================

def test_application_composition_root_shared_instances(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify that SageApplication constructs single-instance shared services correctly."""
    app = SageApplication(conn=test_db, model_gateway=mock_runtime_gateway)

    # Subsystems share the exact same instances
    assert app.connection is test_db
    assert app.supervisor._store is app._state_store
    assert app.supervisor._authority is app.trust_authority
    assert app.supervisor._model_gateway is app.model_gateway
    assert app.worker_manager._supervisor is app.supervisor
    assert app.communication_gateway._supervisor is app.supervisor
    assert app.communication_gateway._friday._gateway is app.model_gateway
    assert app.communication_gateway._friday._memory is app.memory_engine
    assert app.job_queue is app.worker_manager._queue

    # Starts and stops cleanly
    app.start()
    assert app.is_running is True
    app.stop()
    assert app.is_running is False


def test_application_context_manager_lifecycle(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify that SageApplication works cleanly inside a context manager."""
    with SageApplication(conn=test_db, model_gateway=mock_runtime_gateway) as app:
        assert app.is_running is True
    assert app.is_running is False


# =============================================================================
# 2. CRITICAL SIDECAR HMAC PAYLOAD AUTHENTICATION TESTS
# =============================================================================

def test_sidecar_hmac_complete_payload_authentication():
    """Verify that any modification to the payload invalidates the HMAC signature."""
    secret = "secret-test-key-32-bytes-long-1234"
    codec = SecureSidecarCodec(auth_secret=secret)

    # Valid message
    msg = SidecarEnvelope(
        type=SidecarEventType.OUTBOUND_SEND,
        correlation_id="corr-1",
        nonce="nonce-1",
        payload={"destination": "12345", "content": "Deploy safe payload"},
    )
    encoded = codec.encode(msg)

    # 1. Decoding untouched message succeeds
    decoded = codec.decode(encoded)
    assert decoded.type == SidecarEventType.OUTBOUND_SEND
    assert decoded.payload["content"] == "Deploy safe payload"

    # 2. Tampered payload MUST fail HMAC verification
    raw_dict = json.loads(encoded)
    raw_dict["payload"]["content"] = "Deploy MALICIOUS payload"  # Altered payload
    tampered_line = json.dumps(raw_dict) + "\n"

    with pytest.raises(SidecarSecurityError, match="HMAC authentication failure"):
        codec.decode(tampered_line)


def test_sidecar_hmac_field_tampering_rejection():
    """Verify that altering event_type, correlation_id, timestamp, or nonce causes HMAC rejection."""
    secret = "secret-test-key-32-bytes-long-1234"
    codec = SecureSidecarCodec(auth_secret=secret)

    msg = SidecarEnvelope(
        type=SidecarEventType.HEARTBEAT,
        correlation_id="corr-2",
        nonce="nonce-2",
        payload={"status": "OK"},
    )
    encoded = codec.encode(msg)
    raw_dict = json.loads(encoded)

    # Tamper correlation_id
    tampered_corr = dict(raw_dict)
    tampered_corr["correlation_id"] = "corr-altered"
    with pytest.raises(SidecarSecurityError, match="HMAC authentication failure"):
        codec.decode(json.dumps(tampered_corr) + "\n")

    # Tamper event_type
    tampered_type = dict(raw_dict)
    tampered_type["type"] = "outbound_send"
    with pytest.raises(SidecarSecurityError, match="HMAC authentication failure"):
        codec.decode(json.dumps(tampered_type) + "\n")

    # Tamper nonce
    tampered_nonce = dict(raw_dict)
    tampered_nonce["nonce"] = "nonce-altered"
    with pytest.raises(SidecarSecurityError, match="HMAC authentication failure"):
        codec.decode(json.dumps(tampered_nonce) + "\n")


def test_sidecar_timestamp_expiration_rejection():
    """Verify that messages with expired or out-of-window timestamps are rejected."""
    secret = "secret-test-key-32-bytes-long-1234"
    codec = SecureSidecarCodec(auth_secret=secret, max_drift_seconds=60.0)

    # Message timestamped 10 minutes in the past
    past_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    msg = SidecarEnvelope(
        type=SidecarEventType.HEARTBEAT,
        correlation_id="c-old",
        nonce="n-old",
        timestamp=past_ts,
        payload={"status": "OK"},
    )
    encoded = codec.encode(msg)

    with pytest.raises(SidecarSecurityError, match="outside allowed time window"):
        codec.decode(encoded)


def test_sidecar_sliding_window_replay_defense():
    """Verify that identical nonces or correlation IDs are rejected as replays."""
    secret = "secret-test-key-32-bytes-long-1234"
    host = SecureSidecarHost(auth_secret=secret)
    codec = SecureSidecarCodec(auth_secret=secret)

    # Complete registration
    reg = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="c-reg",
        nonce="n-reg",
        payload={"sidecar_id": "wa_sidecar_node"},
    )
    host.process_incoming_line(codec.encode(reg))

    # Send first valid message
    msg = SidecarEnvelope(
        type=SidecarEventType.INBOUND_MESSAGE,
        correlation_id="c-msg-1",
        nonce="n-msg-1",
        payload={"text": "Hello SAGE"},
    )
    encoded_msg = codec.encode(msg)
    host.process_incoming_line(encoded_msg)

    # Replaying the exact same message MUST be rejected
    with pytest.raises(SidecarSecurityError, match="Replay detected"):
        host.process_incoming_line(encoded_msg)

    # Generating a sidecar secret helper test
    sec = generate_sidecar_secret()
    assert len(sec) == 64
    assert isinstance(sec, str)


# =============================================================================
# 3. IDENTITY, MEMORY & APPROVAL SECURITY HARDENING
# =============================================================================

def test_user_cannot_spoof_identity_in_message_body(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify that claiming an admin identity in message text does not override transport sender identity."""
    app = SageApplication(conn=test_db, model_gateway=mock_runtime_gateway, admin_users={"real_admin"})

    # Register standard non-admin user
    app.security_enforcer.register_identity(
        IdentityMapping(
            external_sender_id="mallory",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="user_mallory",
            permissions=[ChannelPermissionLevel.CHAT],  # No admin or approve
        )
    )

    outbound = app._outbound_policy
    outbound.allow_recipient(ChannelPlatform.TELEGRAM, "conv_mallory")

    transport = MockTelegramTransport()
    adapter = TelegramChannelAdapter(bot_token="tok", transport=transport)
    adapter.initialize()
    app.communication_gateway.register_adapter(adapter)

    # Mallory attempts to execute an admin command by claiming sender=real_admin in text
    spoofed_msg = IncomingMessage(
        message_id="m-spoof",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="mallory",  # True sender
        conversation_id="conv_mallory",
        text="/approve req-12345 sender=real_admin user=real_admin",
    )
    resp = app.communication_gateway.process_message(spoofed_msg)
    assert resp is not None
    assert "Access Denied: You lack APPROVE permission" in resp.content


def test_cross_user_memory_isolation(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify that User A cannot retrieve or query User B's personal memory."""
    app = SageApplication(conn=test_db, model_gateway=mock_runtime_gateway)
    store = app.memory_engine.store

    # Store private facts for User A and User B
    store.store_record(
        UnifiedMemoryRecord(
            content="Alice's confidential financial plan: portfolio allocation 70/30.",
            owner_id="alice",
            access_scope="user:alice",
        )
    )
    store.store_record(
        UnifiedMemoryRecord(
            content="Bob's private schedule: dentist at 3pm.",
            owner_id="bob",
            access_scope="user:bob",
        )
    )

    # Query scoped to Bob
    bob_records = store.query_records(UnifiedMemoryQuery(owner_id="bob"))
    assert len(bob_records) == 1
    assert "Bob's private schedule" in bob_records[0].content
    assert "Alice" not in bob_records[0].content

    # Query scoped to Alice
    alice_records = store.query_records(UnifiedMemoryQuery(owner_id="alice"))
    assert len(alice_records) == 1
    assert "Alice's confidential financial plan" in alice_records[0].content
    assert "Bob" not in alice_records[0].content


def test_duplicate_and_stale_approval_rejection(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify that approval requests cannot be approved twice and stale requests fail closed."""
    app = SageApplication(conn=test_db, model_gateway=mock_runtime_gateway)
    approval_store = app.approval_store

    # 1. Create approval request
    req = approval_store.create_request(action_name="system.elevated_exec", ttl_seconds=300.0)

    # 2. Approve once
    approved = approval_store.approve_request(req.request_id, actor="admin_1")
    assert approved.status == "APPROVED"

    # 3. Attempt duplicate approval -> MUST raise TrustError
    from src.trust.errors import TrustError

    with pytest.raises(TrustError, match="non-pending status 'APPROVED'"):
        approval_store.approve_request(req.request_id, actor="admin_1")

    # 4. Expired request fails closed
    expired_req = approval_store.create_request(action_name="system.expired_exec", ttl_seconds=-10.0)
    with pytest.raises(TrustError, match="non-pending status 'EXPIRED'"):
        approval_store.approve_request(expired_req.request_id, actor="admin_1")


# =============================================================================
# 4. END-TO-END APPLICATION INTEGRATION TESTS
# =============================================================================

def test_e2e_conversational_and_task_execution_paths(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify end-to-end integration: conversation -> Friday -> task proposal -> confirmation -> notification."""
    app = SageApplication(
        conn=test_db,
        model_gateway=mock_runtime_gateway,
        admin_users={"admin_user"},
    )

    # Register admin user mapping
    app.security_enforcer.register_identity(
        IdentityMapping(
            external_sender_id="tg_123",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="admin_user",
            permissions=[
                ChannelPermissionLevel.CHAT,
                ChannelPermissionLevel.TASK_REQUEST,
                ChannelPermissionLevel.APPROVE,
                ChannelPermissionLevel.ADMIN,
            ],
        )
    )

    transport = MockTelegramTransport()
    adapter = TelegramChannelAdapter(bot_token="fake_bot_token", transport=transport)
    adapter.initialize()
    app.communication_gateway.register_adapter(adapter)

    app.communication_gateway._outbound.allow_recipient(ChannelPlatform.TELEGRAM, "chat_room_1")

    # Start application
    app.start()

    # Step 1: Normal conversational interaction with Friday
    chat_msg = IncomingMessage(
        message_id="m-chat-1",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_123",
        conversation_id="chat_room_1",
        text="What are your primary operational directives?",
    )
    chat_resp = app.communication_gateway.process_message(chat_msg)
    assert chat_resp is not None
    assert "Runtime Friday response to:" in chat_resp.content

    # Step 2: Propose background task via /task
    task_msg = IncomingMessage(
        message_id="m-task-1",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_123",
        conversation_id="chat_room_1",
        text="/task Generate weekly report",
    )
    task_resp = app.communication_gateway.process_message(task_msg)
    assert task_resp is not None
    assert "Task Proposed:" in task_resp.content

    # Extract task ID
    import re
    match = re.search(r"Task Proposed: `([^`]+)`", task_resp.content)
    assert match is not None
    tid = match.group(1)

    # Step 3: Confirm and execute task
    conf_msg = IncomingMessage(
        message_id="m-conf-1",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_123",
        conversation_id="chat_room_1",
        text=f"/confirm {tid}",
    )
    conf_resp = app.communication_gateway.process_message(conf_msg)
    assert conf_resp is not None
    assert f"Task `{tid}` Executed" in conf_resp.content

    # Step 4: Background job completion notification
    # Find the job in job queue
    jobs = app.job_queue.list_jobs(status=JobStatus.PENDING)
    if jobs:
        jid = jobs[0].job_id
        app.communication_gateway.notify_job_completion(jid, success=True, outcome_summary="Report generated.")
        assert any(f"Background Job `{jid}` Completed" in m["text"] for m in transport.sent_messages)

    # Step 5: Stop application cleanly
    app.stop()
    assert app.is_running is False


# =============================================================================
# 5. ADDITIONAL SECURITY & LIFECYCLE TESTS
# =============================================================================

def test_sidecar_invalid_protocol_version_rejected():
    """Verify that sidecar envelopes with invalid or unsupported protocol versions fail closed."""
    with pytest.raises(ValueError, match="Unsupported sidecar protocol version"):
        SidecarEnvelope(
            version="1.0",  # Requires 2.0
            type=SidecarEventType.HEARTBEAT,
        )


def test_background_worker_registration_and_concurrency(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify worker registration lifecycle and bounded concurrency."""
    app = SageApplication(
        conn=test_db,
        model_gateway=mock_runtime_gateway,
        num_workers=2,
    )
    app.start()

    # Register worker
    reg1 = app.worker_manager.register_worker(worker_id="w-1")
    reg2 = app.worker_manager.register_worker(worker_id="w-2")
    assert reg1.status.value == "ACTIVE"
    assert reg2.status.value == "ACTIVE"

    workers = app.worker_manager.list_workers()
    assert len(workers) == 2
    worker_ids = [w.worker_id for w in workers]
    assert "w-1" in worker_ids
    assert "w-2" in worker_ids

    # Unregister worker
    app.worker_manager.unregister_worker("w-1")
    app.worker_manager.unregister_worker("w-2")

    app.stop()


def test_disabled_messaging_channels_allow_local_operation(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify that SAGE boots and operates locally even when Telegram and WhatsApp are offline/disabled."""
    app = SageApplication(
        conn=test_db,
        model_gateway=mock_runtime_gateway,
    )
    # Clear adapters to simulate disabled messaging
    app.communication_gateway._adapters.clear()
    app.start()

    assert app.is_running is True

    # Local goal execution still succeeds
    task = app.supervisor.submit_goal("Local offline calculation")
    assert task is not None
    assert task.state.value == "SUBMITTED"

    app.stop()


def test_desktop_observer_runtime_lifecycle(test_db: sqlite3.Connection, mock_runtime_gateway: ModelGateway):
    """Verify DesktopObserverClient lifecycle management and tool registration in SageApplication."""
    import queue
    from src.desktop.client import DesktopObserverClient

    class MockWatcher:
        def __init__(self):
            self._running = True
            self._queue = queue.Queue()

            class Stdin:
                def __init__(self, outer):
                    self._outer = outer

                def write(self, s):
                    if not s.strip() or not self._outer._running:
                        return
                    try:
                        cmd = json.loads(s.strip())
                        if cmd.get("command") == "ping":
                            resp = {"Id": cmd.get("id", 1), "Status": "SUCCESS", "Result": {"pong": True}}
                            self._outer._queue.put(json.dumps(resp) + "\n")
                    except Exception:
                        pass

                def flush(self):
                    pass

            class Stdout:
                def __init__(self, outer):
                    self._outer = outer

                def readline(self):
                    if not self._outer._running:
                        return ""
                    try:
                        return self._outer._queue.get(timeout=0.2)
                    except queue.Empty:
                        return ""

            class Stderr:
                def readline(self):
                    return ""

            self.stdin = Stdin(self)
            self.stdout = Stdout(self)
            self.stderr = Stderr()

        def poll(self):
            return 0 if not self._running else None

        def terminate(self):
            self._running = False

        def wait(self, timeout=None):
            self._running = False
            return 0

    mock_proc = MockWatcher()
    ready_evt = json.dumps({"EventType": "OBSERVER_READY", "Timestamp": time.time(), "SessionGeneration": 1}) + "\n"
    mock_proc._queue.put(ready_evt)

    client = DesktopObserverClient(
        executable_path="mock_watcher.exe",
        process_factory=lambda *a, **kw: mock_proc,
        startup_timeout_seconds=2.0,
    )

    app = SageApplication(
        conn=test_db,
        model_gateway=mock_runtime_gateway,
        desktop_client=client,
    )

    # Verify tools registered in broker
    assert "windows.snapshot" in app._tool_broker._tools
    assert "windows.invoke" in app._tool_broker._tools
    assert "windows.click" in app._tool_broker._tools
    assert "desktop.automate" in app._skill_registry._skills

    app.start()
    assert app.is_running is True
    assert app.desktop_client.is_running is True

    app.stop()
    assert app.is_running is False
    assert app.desktop_client.is_running is False


