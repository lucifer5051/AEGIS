"""PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol Test Suite.

Verifies:
- Communication domain models & zero-secret invariant.
- SecurityPolicyEnforcer identity resolution, permission checks, and attachment validation.
- SecureSidecarProtocol HMAC authentication, replay defense, and line bounding.
- TelegramChannelAdapter & WhatsAppChannelAdapter normalization and chunking.
- OutboundPolicyEngine recipient validation and rate limiting.
- FridayConversationHandler routing through ModelGateway and UnifiedMemoryEngine.
- CommunicationGateway command handling, group approval isolation, and background job notifications.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.communication.adapters.telegram import TelegramChannelAdapter
from src.communication.adapters.whatsapp import WhatsAppChannelAdapter
from src.communication.domain import (
    ChannelPermissionLevel,
    ChannelPlatform,
    ConversationType,
    IdentityMapping,
    IncomingMessage,
    MessageAttachment,
    OutgoingMessage,
)
from src.communication.errors import (
    AttachmentPolicyError,
    ChannelAuthenticationError,
    ChannelAuthorizationError,
    RateLimitExceededError,
    SidecarSecurityError,
    UnauthorizedSenderError,
)
from src.communication.friday import FridayConversationHandler
from src.communication.gateway import CommunicationGateway
from src.communication.outbound import OutboundPolicyEngine
from src.communication.security import SecurityPolicyEnforcer
from src.communication.sidecar import (
    PROTOCOL_VERSION,
    SecureSidecarCodec,
    SecureSidecarHost,
    SidecarEnvelope,
    SidecarEventType,
)
from src.gateway.transport import MockTelegramTransport
from src.manager.models import StepPlan, TaskState
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
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
from src.scheduler.job_models import BackgroundJob, JobStatus
from src.scheduler.job_queue import JobQueue
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.trust.approval import ApprovalStore
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


class MockProvider(BaseModelProvider):
    def __init__(self, provider_id: str = "mock-friday") -> None:
        super().__init__(provider_id=provider_id, provider_type=ProviderType.LOCAL)
        self.default_model = "mock-friday-v1"

    def list_models(self) -> List[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id=self.default_model,
                provider_id=self.provider_id,
                display_name="Mock Friday Model",
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
            content=f"Friday response to: {request.prompt}",
            model_id=self.default_model,
            provider_id=self.provider_id,
        )

    def generate_stream(self, request: ModelRequest):
        yield None

    def check_health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderHealthStatus.HEALTHY)


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "comm_test.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def mock_gateway() -> ModelGateway:
    registry = ModelRegistry()
    adapter = MockProvider()
    registry.register_provider(adapter)
    registry.set_alias("fast", "mock-friday-v1")
    registry.set_alias("balanced", "mock-friday-v1")

    router = ModelRouter(registry)
    return ModelGateway(registry=registry, router=router)


# =============================================================================
# 1. DOMAIN & ZERO-SECRET INVARIANT
# =============================================================================

def test_incoming_and_outgoing_zero_secrets():
    """Verify that messages with embedded raw API keys are rejected at validation time."""
    with pytest.raises(Exception):
        IncomingMessage(
            message_id="msg-1",
            channel=ChannelPlatform.TELEGRAM,
            sender_id="123",
            conversation_id="123",
            text="Here is my token sk-123456789012345678901234",
        )

    with pytest.raises(Exception):
        OutgoingMessage(
            channel=ChannelPlatform.TELEGRAM,
            destination="123",
            content="Here is secret: ghp_123456789012345678901234567890",
        )


# =============================================================================
# 2. SECURITY POLICY ENFORCER
# =============================================================================

def test_security_enforcer_sender_resolution():
    enforcer = SecurityPolicyEnforcer()
    mapping = IdentityMapping(
        external_sender_id="user_alice",
        channel=ChannelPlatform.TELEGRAM,
        sage_user_id="sage_user_1",
        display_name="Alice",
        permissions=[ChannelPermissionLevel.RECEIVE, ChannelPermissionLevel.CHAT],
    )
    enforcer.register_identity(mapping)

    msg = IncomingMessage(
        message_id="m1",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="user_alice",
        conversation_id="conv_1",
        text="Hello",
    )
    ctx = enforcer.resolve_context(msg)
    assert ctx.user_id == "sage_user_1"
    assert ctx.has_permission(ChannelPermissionLevel.CHAT)
    assert not ctx.has_permission(ChannelPermissionLevel.APPROVE)


def test_security_enforcer_unauthorized_sender_fails_closed():
    enforcer = SecurityPolicyEnforcer()
    msg = IncomingMessage(
        message_id="m2",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="unregistered_user",
        conversation_id="conv_1",
        text="Hello",
    )
    with pytest.raises(UnauthorizedSenderError):
        enforcer.resolve_context(msg)


def test_security_enforcer_group_isolation():
    enforcer = SecurityPolicyEnforcer(admin_users={"admin_user"})
    mapping = IdentityMapping(
        external_sender_id="admin_external",
        channel=ChannelPlatform.TELEGRAM,
        sage_user_id="admin_user",
        permissions=[ChannelPermissionLevel.ADMIN, ChannelPermissionLevel.APPROVE, ChannelPermissionLevel.CHAT],
    )
    enforcer.register_identity(mapping)
    enforcer.allow_group(ChannelPlatform.TELEGRAM, "group_allowed")

    # In group chat, admin/approve permissions are stripped
    group_msg = IncomingMessage(
        message_id="m3",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="admin_external",
        conversation_id="group_allowed",
        conversation_type=ConversationType.GROUP,
        text="Group command",
    )
    ctx = enforcer.resolve_context(group_msg)
    assert not ctx.has_permission(ChannelPermissionLevel.ADMIN)
    assert not ctx.has_permission(ChannelPermissionLevel.APPROVE)
    assert ctx.has_permission(ChannelPermissionLevel.CHAT)


def test_security_enforcer_attachment_validation():
    enforcer = SecurityPolicyEnforcer()

    # Valid text file
    att = MessageAttachment(
        filename="report.txt",
        content_type="text/plain",
        size_bytes=100,
    )
    validated = enforcer.validate_attachment(att, raw_bytes=b"Sample content")
    assert validated.is_validated is True
    assert validated.sha256 is not None

    # Path traversal rejection
    bad_att = MessageAttachment(
        filename="../etc/passwd.txt",
        content_type="text/plain",
        size_bytes=100,
    )
    with pytest.raises(AttachmentPolicyError):
        enforcer.validate_attachment(bad_att)

    # Prohibited executable extension
    exe_att = MessageAttachment(
        filename="malware.exe",
        content_type="application/octet-stream",
        size_bytes=100,
    )
    with pytest.raises(AttachmentPolicyError):
        enforcer.validate_attachment(exe_att)


# =============================================================================
# 3. SECURE SIDECAR PROTOCOL
# =============================================================================

def test_sidecar_handshake_and_authentication():
    secret = "test-ipc-secret-key"
    host = SecureSidecarHost(auth_secret=secret)
    codec = SecureSidecarCodec(auth_secret=secret)

    # Unregistered event fails
    heartbeat = SidecarEnvelope(
        type=SidecarEventType.HEARTBEAT,
        correlation_id="c-1",
    )
    encoded_hb = codec.encode(heartbeat)
    with pytest.raises(SidecarSecurityError):
        host.process_incoming_line(encoded_hb)

    # Registration handshake
    reg_msg = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="c-reg",
        payload={"sidecar_id": "baileys_node_1"},
    )
    encoded_reg = codec.encode(reg_msg)
    res = host.process_incoming_line(encoded_reg)
    assert res.type == SidecarEventType.REGISTER
    assert host.is_registered is True

    # Replay detection on same correlation ID
    with pytest.raises(SidecarSecurityError):
        host.process_incoming_line(encoded_reg)

    # Subsequent heartbeat succeeds
    hb_msg = SidecarEnvelope(
        type=SidecarEventType.HEARTBEAT,
        correlation_id="c-2",
    )
    encoded_hb2 = codec.encode(hb_msg)
    hb_res = host.process_incoming_line(encoded_hb2)
    assert hb_res.type == SidecarEventType.HEARTBEAT


def test_sidecar_tampered_signature_rejection():
    secret = "test-ipc-secret-key"
    host = SecureSidecarHost(auth_secret=secret)
    bad_codec = SecureSidecarCodec(auth_secret="wrong-secret")

    reg_msg = SidecarEnvelope(
        type=SidecarEventType.REGISTER,
        correlation_id="c-bad",
        payload={"sidecar_id": "rogue_sidecar"},
    )
    bad_line = bad_codec.encode(reg_msg)
    with pytest.raises(SidecarSecurityError, match="HMAC authentication failure"):
        host.process_incoming_line(bad_line)


# =============================================================================
# 4. TELEGRAM ADAPTER & CHUNKING
# =============================================================================

def test_telegram_adapter_poll_and_chunk():
    mock_transport = MockTelegramTransport()
    # Add a mock raw update
    mock_transport.enqueue_update(
        {
            "update_id": 101,
            "message": {
                "message_id": 101,
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 123, "is_bot": False, "username": "testuser"},
                "text": "Hello Telegram",
            },
        }
    )

    adapter = TelegramChannelAdapter(
        bot_token="fake_bot_token",
        transport=mock_transport,
    )
    adapter.initialize()
    assert adapter.health_check().is_connected is True

    inbound = adapter.poll_inbound()
    assert len(inbound) == 1
    assert inbound[0].text == "Hello Telegram"
    assert inbound[0].sender_id == "123"

    # Deduplication test: polling again yields nothing
    mock_transport.enqueue_update(
        {
            "update_id": 101,  # Same ID
            "message": {
                "message_id": 101,
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 123, "is_bot": False, "username": "testuser"},
                "text": "Hello Telegram",
            },
        }
    )
    assert len(adapter.poll_inbound()) == 0

    # Chunking test for long message (>4096 chars)
    long_text = "A" * 5000
    out = OutgoingMessage(
        channel=ChannelPlatform.TELEGRAM,
        destination="555",
        content=long_text,
    )
    res = adapter.send_message(out)
    assert res.success is True
    # Verify transport received two chunks
    assert len(mock_transport.sent_messages) == 2


# =============================================================================
# 5. WHATSAPP ADAPTER & CVE-2026-48063 MITIGATION
# =============================================================================

def test_whatsapp_adapter_cve_and_normalization():
    mock_transport = MagicMock()
    # Event 1: Malicious placeholder requestId (CVE-2026-48063)
    # Event 2: fromMe=True (message from self)
    # Event 3: Valid user message
    mock_transport.get_events.return_value = [
        {"requestId": "cve-placeholder-payload"},
        {"key": {"remoteJid": "12345@s.whatsapp.net", "id": "msg-self", "fromMe": True}, "message": {"conversation": "Self"}},
        {"key": {"remoteJid": "999888@s.whatsapp.net", "id": "msg-valid", "fromMe": False}, "message": {"conversation": "Valid WhatsApp"}},
    ]

    adapter = WhatsAppChannelAdapter(transport=mock_transport)
    adapter.initialize()

    messages = adapter.poll_inbound()
    assert len(messages) == 1
    assert messages[0].message_id == "msg-valid"
    assert messages[0].sender_id == "999888"
    assert messages[0].text == "Valid WhatsApp"


# =============================================================================
# 6. OUTBOUND POLICY ENGINE
# =============================================================================

def test_outbound_policy_rate_limit_and_recipient():
    engine = OutboundPolicyEngine(
        allowed_recipients={(ChannelPlatform.TELEGRAM, "allowed_user")},
        max_rate_per_minute=2,
    )

    valid_out = OutgoingMessage(
        channel=ChannelPlatform.TELEGRAM,
        destination="allowed_user",
        content="Hello!",
    )
    # First 2 sends succeed
    s1 = engine.validate_and_sanitize(valid_out)
    s2 = engine.validate_and_sanitize(valid_out)
    assert s1.content == "Hello!"

    # 3rd send violates rate limit
    with pytest.raises(RateLimitExceededError):
        engine.validate_and_sanitize(valid_out)

    # Unregistered recipient denied
    unreg_out = OutgoingMessage(
        channel=ChannelPlatform.TELEGRAM,
        destination="rogue_user",
        content="Secret spam",
    )
    with pytest.raises(ChannelAuthorizationError):
        engine.validate_and_sanitize(unreg_out)


# =============================================================================
# 7. FRIDAY CONVERSATION & MEMORY INTEGRATION
# =============================================================================

def test_friday_conversation_with_memory(mock_gateway: ModelGateway, test_db: sqlite3.Connection):
    from src.memory.unified_store import UnifiedMemoryStore

    store = UnifiedMemoryStore(test_db)
    memory_mock = MagicMock()
    memory_mock.store = store

    # Pre-save a memory for user_1
    pre_mem = UnifiedMemoryRecord(
        content="User preferred language is Python.",
        owner_id="user_1",
        access_scope="user:user_1",
    )
    store.store_record(pre_mem)

    friday = FridayConversationHandler(model_gateway=mock_gateway, memory_engine=memory_mock)

    enforcer = SecurityPolicyEnforcer()
    enforcer.register_identity(
        IdentityMapping(
            external_sender_id="ext_user_1",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="user_1",
            permissions=[ChannelPermissionLevel.CHAT, ChannelPermissionLevel.MEMORY_ACCESS],
        )
    )

    msg = IncomingMessage(
        message_id="msg-conv-1",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="ext_user_1",
        conversation_id="conv-1",
        text="What should we build today?",
    )
    ctx = enforcer.resolve_context(msg)
    reply = friday.handle_message(ctx, msg)

    assert "Friday response to: What should we build today?" in reply

    # Verify episodic conversation turn was recorded for user_1
    records = store.query_records(UnifiedMemoryQuery(query_text="What should we build today", owner_id="user_1"))
    assert len(records) > 0
    assert "User: What should we build today?" in records[0].content


# =============================================================================
# 8. FULL COMMUNICATION GATEWAY & APPROVAL ISOLATION
# =============================================================================

def test_communication_gateway_full_flow(
    test_db: sqlite3.Connection, mock_gateway: ModelGateway
):
    authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    state_store = StateStore(test_db)
    approval_store = ApprovalStore(test_db)
    broker = ToolBroker(authority)
    supervisor = Supervisor(state_store=state_store, trust_authority=authority, tool_broker=broker)
    job_queue = JobQueue(test_db)

    enforcer = SecurityPolicyEnforcer(admin_users={"admin_1"})
    # Register admin user
    enforcer.register_identity(
        IdentityMapping(
            external_sender_id="tg_admin",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="admin_1",
            permissions=[
                ChannelPermissionLevel.CHAT,
                ChannelPermissionLevel.TASK_REQUEST,
                ChannelPermissionLevel.APPROVE,
                ChannelPermissionLevel.ADMIN,
            ],
        )
    )
    # Register normal user
    enforcer.register_identity(
        IdentityMapping(
            external_sender_id="tg_user",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="user_2",
            permissions=[ChannelPermissionLevel.CHAT, ChannelPermissionLevel.TASK_REQUEST],
        )
    )
    enforcer.allow_group(ChannelPlatform.TELEGRAM, "tg_group_1")

    outbound_policy = OutboundPolicyEngine()
    outbound_policy.allow_recipient(ChannelPlatform.TELEGRAM, "chat_admin")
    outbound_policy.allow_recipient(ChannelPlatform.TELEGRAM, "tg_group_1")

    friday = FridayConversationHandler(model_gateway=mock_gateway)
    transport = MockTelegramTransport()
    adapter = TelegramChannelAdapter(bot_token="fake_token", transport=transport)
    adapter.initialize()

    gateway = CommunicationGateway(
        security_enforcer=enforcer,
        friday_handler=friday,
        outbound_policy=outbound_policy,
        adapters={ChannelPlatform.TELEGRAM: adapter},
        supervisor=supervisor,
        approval_store=approval_store,
        job_queue=job_queue,
    )

    # 1. Normal chat message
    chat_msg = IncomingMessage(
        message_id="c-1",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_admin",
        conversation_id="chat_admin",
        text="Good morning Friday",
    )
    resp = gateway.process_message(chat_msg)
    assert resp is not None
    assert "Friday response to: Good morning Friday" in resp.content

    # 2. Propose task
    task_msg = IncomingMessage(
        message_id="c-2",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_admin",
        conversation_id="chat_admin",
        text="/task Build telemetry widget",
    )
    task_resp = gateway.process_message(task_msg)
    assert task_resp is not None
    assert "Task Proposed:" in task_resp.content

    # 3. Create approval request and attempt approval
    app_req = approval_store.create_request(action_name="system.deploy", task_id="t-100")

    # In group chat, approval MUST be rejected by security invariant
    group_app_msg = IncomingMessage(
        message_id="c-3",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_admin",
        conversation_id="tg_group_1",
        conversation_type=ConversationType.GROUP,
        text=f"/approve {app_req.request_id}",
    )
    group_resp = gateway.process_message(group_app_msg)
    assert group_resp is not None
    assert "Approvals cannot be executed in group chats" in group_resp.content

    # In private chat, approval succeeds
    priv_app_msg = IncomingMessage(
        message_id="c-4",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_admin",
        conversation_id="chat_admin",
        conversation_type=ConversationType.PRIVATE,
        text=f"/approve {app_req.request_id}",
    )
    priv_resp = gateway.process_message(priv_app_msg)
    assert priv_resp is not None
    assert "Approved" in priv_resp.content
    assert approval_store.get_request(app_req.request_id).status == "APPROVED"

    # 4. Background job completion notification
    # Enqueue a job and notify
    job = BackgroundJob(
        job_id="job-999",
        task_id="t-100",
        goal_text="Build widget",
        action_type="build",
        status=JobStatus.COMPLETED,
    )
    job_queue.enqueue_job(job)
    gateway._job_notifications["job-999"] = (ChannelPlatform.TELEGRAM, "chat_admin", "admin_1")

    res = gateway.notify_job_completion("job-999", success=True, outcome_summary="Built successfully")
    assert res is not None
    assert res.success is True
    # Transport received the completion notification
    assert any("Background Job `job-999` Completed" in m["text"] for m in transport.sent_messages)


# =============================================================================
# 9. EXTENDED SIDECAR SECURITY & PROTOCOL BOUNDS
# =============================================================================

def test_sidecar_oversized_line_rejected():
    codec = SecureSidecarCodec()
    huge_line = "A" * (65536 + 10) + "\n"
    with pytest.raises(SidecarSecurityError, match="exceeds 65536 limit"):
        codec.decode(huge_line)


def test_sidecar_malformed_json_rejected():
    codec = SecureSidecarCodec()
    bad_line = "{not-json-at-all\n"
    with pytest.raises(SidecarSecurityError, match="Malformed JSON"):
        codec.decode(bad_line)


def test_sidecar_empty_line_rejected():
    codec = SecureSidecarCodec()
    with pytest.raises(SidecarSecurityError, match="empty or whitespace-only"):
        codec.decode("   \n")


# =============================================================================
# 10. WHATSAPP GROUP REJECTION & SECURITY INVARIANTS
# =============================================================================

def test_whatsapp_group_rejection_by_default():
    enforcer = SecurityPolicyEnforcer()
    mapping = IdentityMapping(
        external_sender_id="111222",
        channel=ChannelPlatform.WHATSAPP,
        sage_user_id="user_wa",
    )
    enforcer.register_identity(mapping)

    group_msg = IncomingMessage(
        message_id="wa-g-1",
        channel=ChannelPlatform.WHATSAPP,
        sender_id="111222",
        conversation_id="123456789@g.us",
        conversation_type=ConversationType.GROUP,
        text="Group msg",
    )
    with pytest.raises(UnauthorizedSenderError, match="not authorized"):
        enforcer.resolve_context(group_msg)


# =============================================================================
# 11. GATEWAY COMMANDS: CONFIRM, CANCEL, STATUS
# =============================================================================

def test_gateway_confirm_and_cancel_commands(test_db: sqlite3.Connection, mock_gateway: ModelGateway):
    authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    state_store = StateStore(test_db)
    approval_store = ApprovalStore(test_db)
    broker = ToolBroker(authority)
    supervisor = Supervisor(state_store=state_store, trust_authority=authority, tool_broker=broker)

    enforcer = SecurityPolicyEnforcer()
    enforcer.register_identity(
        IdentityMapping(
            external_sender_id="tg_dev",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="dev_1",
            permissions=[ChannelPermissionLevel.CHAT, ChannelPermissionLevel.TASK_REQUEST],
        )
    )

    outbound = OutboundPolicyEngine()
    outbound.allow_recipient(ChannelPlatform.TELEGRAM, "dev_chat")

    friday = FridayConversationHandler(model_gateway=mock_gateway)
    transport = MockTelegramTransport()
    adapter = TelegramChannelAdapter(bot_token="test_tok", transport=transport)
    adapter.initialize()

    gateway = CommunicationGateway(
        security_enforcer=enforcer,
        friday_handler=friday,
        outbound_policy=outbound,
        adapters={ChannelPlatform.TELEGRAM: adapter},
        supervisor=supervisor,
        approval_store=approval_store,
    )

    # 1. Check /status
    st_msg = IncomingMessage(
        message_id="m-st",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_dev",
        conversation_id="dev_chat",
        text="/status",
    )
    st_resp = gateway.process_message(st_msg)
    assert st_resp is not None
    assert "SAGE Channel Status:" in st_resp.content

    # 2. Submit task
    t_msg = IncomingMessage(
        message_id="m-task",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_dev",
        conversation_id="dev_chat",
        text="/task Run unit tests",
    )
    t_resp = gateway.process_message(t_msg)
    assert t_resp is not None
    import re
    m = re.search(r"Task Proposed: `([^`]+)`", t_resp.content)
    assert m is not None
    task_id = m.group(1)

    # 3. Confirm task
    c_msg = IncomingMessage(
        message_id="m-conf",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_dev",
        conversation_id="dev_chat",
        text=f"/confirm {task_id}",
    )
    c_resp = gateway.process_message(c_msg)
    assert c_resp is not None
    assert f"Task `{task_id}` Executed" in c_resp.content

    # 4. Propose another task and cancel it
    t2_msg = IncomingMessage(
        message_id="m-task-2",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_dev",
        conversation_id="dev_chat",
        text="/task Long task to cancel",
    )
    t2_resp = gateway.process_message(t2_msg)
    m2 = re.search(r"Task Proposed: `([^`]+)`", t2_resp.content)
    assert m2 is not None
    task_id_2 = m2.group(1)

    cancel_msg = IncomingMessage(
        message_id="m-cancel",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="tg_dev",
        conversation_id="dev_chat",
        text=f"/cancel {task_id_2}",
    )
    cancel_resp = gateway.process_message(cancel_msg)
    assert cancel_resp is not None
    assert f"Task `{task_id_2}` cancelled." in cancel_resp.content


# =============================================================================
# 12. GATEWAY END-TO-END POLLING & PERMISSION DENIAL
# =============================================================================

def test_gateway_poll_and_process_cycle(test_db: sqlite3.Connection, mock_gateway: ModelGateway):
    enforcer = SecurityPolicyEnforcer()
    enforcer.register_identity(
        IdentityMapping(
            external_sender_id="777888",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="user_chatty",
            permissions=[ChannelPermissionLevel.CHAT],
        )
    )

    outbound = OutboundPolicyEngine()
    outbound.allow_recipient(ChannelPlatform.TELEGRAM, "chat_777888")

    friday = FridayConversationHandler(model_gateway=mock_gateway)
    transport = MockTelegramTransport()
    adapter = TelegramChannelAdapter(bot_token="test_tok", transport=transport)
    adapter.initialize()

    gateway = CommunicationGateway(
        security_enforcer=enforcer,
        friday_handler=friday,
        outbound_policy=outbound,
        adapters={ChannelPlatform.TELEGRAM: adapter},
    )

    # Enqueue a message into the transport
    transport.enqueue_update(
        {
            "update_id": 999,
            "message": {
                "message_id": 1,
                "chat": {"id": "chat_777888", "type": "private"},
                "from": {"id": 777888, "is_bot": False, "username": "chatty"},
                "text": "What is the status of my project?",
            },
        }
    )

    results = gateway.poll_and_process()
    assert len(results) == 1
    in_msg, out_msg = results[0]
    assert in_msg.text == "What is the status of my project?"
    assert out_msg is not None
    assert "Friday response to: What is the status of my project?" in out_msg.content
    assert len(transport.sent_messages) == 1


def test_permission_denied_for_unauthorized_task_request(mock_gateway: ModelGateway):
    enforcer = SecurityPolicyEnforcer()
    enforcer.register_identity(
        IdentityMapping(
            external_sender_id="chat_only_user",
            channel=ChannelPlatform.TELEGRAM,
            sage_user_id="user_restricted",
            permissions=[ChannelPermissionLevel.CHAT],  # No TASK_REQUEST
        )
    )

    outbound = OutboundPolicyEngine()
    outbound.allow_recipient(ChannelPlatform.TELEGRAM, "conv_restricted")

    friday = FridayConversationHandler(model_gateway=mock_gateway)
    adapter = TelegramChannelAdapter(bot_token="tok")
    adapter.initialize()

    gateway = CommunicationGateway(
        security_enforcer=enforcer,
        friday_handler=friday,
        outbound_policy=outbound,
        adapters={ChannelPlatform.TELEGRAM: adapter},
    )

    task_msg = IncomingMessage(
        message_id="msg-denied",
        channel=ChannelPlatform.TELEGRAM,
        sender_id="chat_only_user",
        conversation_id="conv_restricted",
        text="/task Propose a restricted action",
    )
    resp = gateway.process_message(task_msg)
    assert resp is not None
    assert "Access Denied: You lack TASK_REQUEST permission." in resp.content


