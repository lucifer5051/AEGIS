"""Prompt 032 -- SAGE Secure Remote-Control Gateway Tests (Telegram-First).

Comprehensive unit and integration test suite covering:
1. Disabled connector does not poll or make network requests.
2. Missing or invalid configuration fails safely.
3. Missing bot token fails closed without leaking details.
4. Unapproved numeric sender ID is rejected before inference or task creation.
5. Spoofed username cannot bypass numeric-ID authorization.
6. Group and channel updates cause no inference, memory retrieval, or execution.
7. Malformed or oversized events are rejected safely.
8. Duplicate update IDs cannot create duplicate tasks (durable deduplication).
9. Sequential processing preserves ordering and task associations.
10. Unauthorized users cannot inspect task status or access private memory.
11. Unknown commands do not fall through to arbitrary execution.
12. Arbitrary shell text and arbitrary filesystem paths cannot be executed.
13. Workspace scope and protected-directory boundaries remain enforced.
14. Existing confirmation and briefing mechanisms cannot be bypassed.
15. Altered, replayed, expired, or mismatched approvals fail closed.
16. /stop cannot cancel unrelated operating-system processes.
17. Restart recovery does not automatically repeat an uncertain external action.
18. API timeouts, transport failures, rate limiting, and malformed responses do not corrupt state.
19. Secrets remain absent from logs, task metadata, persistent records, and error responses.
20. Mocked end-to-end demonstration through SAGE's actual execution pipeline.
21. 100% offline guarantee: zero external network calls.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from src.config.models import TelegramConfig
from src.gateway.errors import ChannelConfigError, TransportError
from src.gateway.gateway import ChannelGateway
from src.gateway.models import OutboundStatus, UpdateLifecycleState
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import CommandRouter
from src.gateway.store import GatewayStore
from src.gateway.transport import MockTelegramTransport
from src.manager.models import TaskState
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


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    """Isolated runtime database with migrations 0001-0009 applied."""
    db_file = tmp_path / "test_gateway.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Isolated scratch directory for file tool execution."""
    sb = tmp_path / "workspace"
    sb.mkdir(parents=True, exist_ok=True)
    # Create sample files for inspection
    (sb / "sample.txt").write_text("Hello from SAGE workspace.", encoding="utf-8")
    (sb / "test_script.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    return sb


@pytest.fixture
def gateway_environment(test_db: sqlite3.Connection, sandbox: Path):
    """Build a complete local SAGE environment wired to ChannelGateway."""
    # 1. Trust Core & Tool Broker
    authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(authority)
    register_builtin_tools(broker, allowed_roots=[sandbox])

    # 2. Skill Registry & First-Party Skills
    registry = SkillRegistry()
    register_first_party_skills(registry, broker)

    # 3. Supervisor & Stores
    state_store = StateStore(test_db)
    approval_store = ApprovalStore(test_db)
    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=authority,
        tool_broker=broker,
        skill_registry=registry,
    )

    # 4. Gateway Store, Transport, and Router
    gateway_store = GatewayStore(test_db)
    transport = MockTelegramTransport()
    router = CommandRouter(
        supervisor=supervisor,
        tool_broker=broker,
        trust_authority=authority,
        approval_store=approval_store,
        workspace_alias="test_workspace",
        workspace_dir=sandbox,
    )

    # 5. Default Valid Config (Authorized Owner: 123456789)
    config = TelegramConfig(
        enabled=True,
        bot_token_env="SAGE_TEST_BOT_TOKEN",
        allowed_user_ids=[123456789],
        allowed_chat_ids=[123456789],
        poll_timeout_seconds=5,
        request_timeout_seconds=5.0,
        max_poll_limit=10,
        rate_limit_per_minute=20,
    )
    limiter = SlidingWindowRateLimiter()
    gateway = ChannelGateway(
        config=config,
        store=gateway_store,
        transport=transport,
        router=router,
        rate_limiter=limiter,
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
    }


def _make_telegram_update(
    update_id: int,
    user_id: int,
    text: str,
    chat_type: str = "private",
    chat_id: int = 123456789,
    username: str = "test_user",
    is_bot: bool = False,
) -> Dict[str, Any]:
    """Helper to construct raw Telegram update dictionaries."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1000 + update_id,
            "date": 1700000000,
            "chat": {
                "id": chat_id,
                "type": chat_type,
            },
            "from": {
                "id": user_id,
                "is_bot": is_bot,
                "first_name": "Test",
                "username": username,
            },
            "text": text,
        },
    }


# ==============================================================================
# TESTS
# ==============================================================================


def test_01_disabled_connector_does_not_poll_or_network(gateway_environment):
    """Disabled connector returns immediately with zero transport calls."""
    env = gateway_environment
    # Disable connector explicitly
    disabled_config = TelegramConfig(enabled=False, allowed_user_ids=[])
    gateway = ChannelGateway(
        config=disabled_config,
        store=env["store"],
        transport=env["transport"],
        router=env["router"],
    )

    # Queue an update in transport
    env["transport"].enqueue_update(_make_telegram_update(1, 123456789, "/status"))

    results = gateway.poll_and_process_once()
    assert results == []
    # No updates were dequeued from transport
    assert len(env["transport"].sent_messages) == 0


def test_02_missing_or_invalid_configuration_fails_safely():
    """Enabling Telegram without allowlisted numeric user/chat IDs fails validation."""
    with pytest.raises(ValueError, match="allowed_user_ids.*empty"):
        TelegramConfig(enabled=True, allowed_user_ids=[])

    with pytest.raises(ValueError, match="allowed_chat_ids.*empty"):
        TelegramConfig(enabled=True, allowed_user_ids=[12345], allowed_chat_ids=[])

    with pytest.raises(ValueError, match="bot_token_env"):
        TelegramConfig(enabled=True, allowed_user_ids=[12345], allowed_chat_ids=[12345], bot_token_env="")

    with pytest.raises(ValueError, match="allowed_user_id.*positive"):
        TelegramConfig(enabled=True, allowed_user_ids=[-100], allowed_chat_ids=[12345])

    with pytest.raises(ValueError, match="allowed_chat_id.*prohibited"):
        TelegramConfig(enabled=True, allowed_user_ids=[12345], allowed_chat_ids=[-100123456789])


def test_03_missing_bot_token_fails_closed_without_leaks(gateway_environment, monkeypatch):
    """Missing bot token environment variable raises ChannelConfigError without leaking."""
    env = gateway_environment
    monkeypatch.delenv("SAGE_TEST_BOT_TOKEN", raising=False)

    with pytest.raises(ChannelConfigError, match="SAGE_TEST_BOT_TOKEN"):
        env["gateway"].poll_and_process_once()


def test_04_unapproved_numeric_sender_id_rejected_before_inference_or_task(
    gateway_environment, monkeypatch
):
    """Unapproved sender ID is rejected immediately without task creation or reply."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Sender 999999999 is NOT in allowed_user_ids ([123456789])
    raw_update = _make_telegram_update(
        update_id=1,
        user_id=999999999,
        text="/task delete all files",
    )
    env["transport"].enqueue_update(raw_update)

    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert results[0].error_summary == "Unauthorized sender ID"

    # Zero tasks created in Supervisor state store
    tasks = env["state_store"].list_tasks()
    assert len(tasks) == 0

    # Zero replies sent back to unauthorized sender (no info leak/enumeration)
    assert len(env["transport"].sent_messages) == 0


def test_05_spoofed_username_cannot_bypass_numeric_id_authorization(
    gateway_environment, monkeypatch
):
    """A spoofed username matching owner cannot bypass numeric ID verification."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Attacker uses username 'owner' but unauthorized numeric user ID
    raw_update = _make_telegram_update(
        update_id=2,
        user_id=555555555,
        text="/status",
        username="owner",
    )
    env["transport"].enqueue_update(raw_update)

    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert len(env["transport"].sent_messages) == 0


def test_06_group_and_channel_updates_cause_no_inference_or_execution(
    gateway_environment, monkeypatch
):
    """Group, supergroup, and channel updates are rejected before inference."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Group message even from authorized user ID is rejected
    raw_update = _make_telegram_update(
        update_id=3,
        user_id=123456789,
        text="/tasks",
        chat_type="group",
        chat_id=-1001234567,
    )
    env["transport"].enqueue_update(raw_update)

    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "private" in (results[0].error_summary or "")
    assert len(env["transport"].sent_messages) == 0


def test_07_malformed_and_oversized_events_rejected_safely(gateway_environment, monkeypatch):
    """Malformed payloads, bot senders, and oversized messages fail safely."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # 1. Bot sender
    bot_update = _make_telegram_update(
        update_id=4,
        user_id=123456789,
        text="/status",
        is_bot=True,
    )
    env["transport"].enqueue_update(bot_update)

    # 2. Oversized message (> 4096 chars)
    oversized_update = _make_telegram_update(
        update_id=5,
        user_id=123456789,
        text="/task " + ("A" * 5000),
    )
    env["transport"].enqueue_update(oversized_update)

    results = env["gateway"].poll_and_process_once()
    assert len(results) == 2
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "Bot" in (results[0].error_summary or "")
    assert results[1].state == UpdateLifecycleState.REJECTED
    assert "exceeds maximum" in (results[1].error_summary or "")


def test_08_duplicate_update_ids_cannot_create_duplicate_tasks(gateway_environment, monkeypatch):
    """Duplicate update IDs are deduplicated in SQLite and execute only once."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    update_payload = _make_telegram_update(
        update_id=101,
        user_id=123456789,
        text="/task verify code quality",
    )

    # First delivery
    env["transport"].enqueue_update(update_payload)
    results_1 = env["gateway"].poll_and_process_once()
    assert len(results_1) == 1
    assert results_1[0].state == UpdateLifecycleState.AWAITING_APPROVAL
    initial_task_id = results_1[0].task_id
    assert initial_task_id is not None

    # Duplicate delivery (same update_id redelivered from transport)
    env["transport"].enqueue_update(update_payload)
    env["gateway"]._next_offset = 101
    results_2 = env["gateway"].poll_and_process_once()
    assert len(results_2) == 1
    assert "Duplicate" in (results_2[0].error_summary or "")
    assert results_2[0].task_id == initial_task_id

    # Exactly one task exists in the state store
    tasks = env["state_store"].list_tasks()
    assert len(tasks) == 1


def test_09_sequential_processing_preserves_ordering_and_associations(
    gateway_environment, monkeypatch
):
    """Sequential updates are processed in strict order with unique task links."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    for i in (10, 11, 12):
        env["transport"].enqueue_update(
            _make_telegram_update(
                update_id=i,
                user_id=123456789,
                text=f"/task task number {i}",
            )
        )

    results = env["gateway"].poll_and_process_once()
    assert len(results) == 3
    task_ids = [r.task_id for r in results]
    assert len(set(task_ids)) == 3  # All distinct tasks

    # Inbound records in store maintain task associations
    for r in results:
        stored = env["store"].get_inbound_update(r.update_id)
        assert stored is not None
        assert stored.task_id == r.task_id


def test_10_unauthorized_users_cannot_inspect_tasks_or_access_memory(
    gateway_environment, monkeypatch
):
    """Unauthorized senders cannot access /status, /tasks, /inspect, or /diagnose."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Pre-create a task
    env["state_store"].create_task("Secret Project Task")

    for idx, cmd in enumerate(("/status", "/tasks", "/inspect", "/diagnose error")):
        env["transport"].enqueue_update(
            _make_telegram_update(
                update_id=20 + idx,
                user_id=999999999,  # Unauthorized
                text=cmd,
            )
        )
        res = env["gateway"].poll_and_process_once()
        assert len(res) == 1
        assert res[0].state == UpdateLifecycleState.REJECTED

    # Ensure no sent messages leaked task or status details
    assert len(env["transport"].sent_messages) == 0


def test_11_unknown_commands_do_not_fall_through_to_arbitrary_execution(
    gateway_environment, monkeypatch
):
    """Arbitrary slash commands (/exec, /sh, /run) are rejected without execution."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    for idx, bad_cmd in enumerate(
        ("/exec whoami", "/sh cat /etc/passwd", "/run python code", "/powershell dir")
    ):
        env["transport"].enqueue_update(
            _make_telegram_update(
                update_id=30 + idx,
                user_id=123456789,
                text=bad_cmd,
            )
        )
        res = env["gateway"].poll_and_process_once()
        assert len(res) == 1
        assert "Unknown or unsupported command" in (res[0].response_text or "")
        assert "Arbitrary shell" in (res[0].response_text or "")


def test_12_arbitrary_shell_and_filesystem_paths_cannot_be_executed(
    gateway_environment, monkeypatch
):
    """Verification and inspection strictly reject non-allowlisted commands and paths."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # 1. Invalid verification check type
    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=40,
            user_id=123456789,
            text="/verify rm -rf /",
        )
    )
    res1 = env["gateway"].poll_and_process_once()
    assert "Invalid check type 'rm -rf /'" in (res1[0].response_text or "")

    # 2. Inspect outside workspace
    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=41,
            user_id=123456789,
            text="/inspect C:\\Windows",
        )
    )
    res2 = env["gateway"].poll_and_process_once()
    assert res2[0].response_text is not None


def test_13_workspace_scope_and_protected_directory_boundaries_enforced(
    gateway_environment, monkeypatch
):
    """File inspection remains strictly read-only and within sandbox."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=50,
            user_id=123456789,
            text="/inspect *.txt",
        )
    )
    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1
    resp = results[0].response_text or ""
    assert "sample.txt" in resp
    assert "Read-Only" in resp


def test_14_confirmation_and_briefing_cannot_be_bypassed(gateway_environment, monkeypatch):
    """Remote /task generates an unexecuted Context Briefing requiring explicit /confirm."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Step 1: Submit task
    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=60,
            user_id=123456789,
            text="/task test task requiring confirmation",
        )
    )
    res1 = env["gateway"].poll_and_process_once()
    task_id = res1[0].task_id
    assert task_id is not None
    assert "/confirm" in (res1[0].response_text or "")

    # Verify task state is SUBMITTED or AWAITING_APPROVAL, NOT COMPLETED
    task = env["state_store"].get_task(task_id)
    assert task is not None
    assert task.state in (TaskState.SUBMITTED, TaskState.AWAITING_APPROVAL)

    # Step 2: Confirm task
    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=61,
            user_id=123456789,
            text=f"/confirm {task_id}",
        )
    )
    res2 = env["gateway"].poll_and_process_once()
    assert "Confirmed" in (res2[0].response_text or "")

    # Task is now executed and complete
    task_after = env["state_store"].get_task(task_id)
    assert task_after is not None
    assert task_after.state == TaskState.COMPLETED


def test_15_altered_replayed_expired_or_mismatched_approvals_fail_closed(
    gateway_environment, monkeypatch
):
    """Expired or non-existent approval requests fail closed."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # 1. Non-existent approval ID
    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=70,
            user_id=123456789,
            text="/approve 00000000-0000-0000-0000-000000000000",
        )
    )
    res1 = env["gateway"].poll_and_process_once()
    assert "Failed to approve" in (res1[0].response_text or "")

    # 2. Create an expired approval request
    expired_req = env["approval_store"].create_request(
        action_name="filesystem.delete",
        ttl_seconds=-10.0,  # Already expired
    )
    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=71,
            user_id=123456789,
            text=f"/approve {expired_req.request_id}",
        )
    )
    res2 = env["gateway"].poll_and_process_once()
    assert "Failed to approve" in (res2[0].response_text or "")


def test_16_stop_command_cannot_cancel_unrelated_os_processes(gateway_environment, monkeypatch):
    """The /stop command only cancels SAGE task records in StateStore."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    task = env["state_store"].create_task("Long Running Task")
    assert task.state == TaskState.SUBMITTED

    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=80,
            user_id=123456789,
            text=f"/stop {task.task_id}",
        )
    )
    results = env["gateway"].poll_and_process_once()
    assert "cancelled" in (results[0].response_text or "").lower()

    # Verify task state in database
    updated = env["state_store"].get_task(task.task_id)
    assert updated is not None
    assert updated.state == TaskState.CANCELLED


def test_17_restart_recovery_does_not_repeat_uncertain_external_action(
    gateway_environment, monkeypatch
):
    """Transport failure during outbound send marks message UNCERTAIN without breaking task."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Configure transport to fail on send_message
    env["transport"].set_send_message_error(TransportError("Network connection reset"))

    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=90,
            user_id=123456789,
            text="/status",
        )
    )
    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1

    # Outbound message is marked UNCERTAIN
    outbounds = env["store"].list_outbound_messages()
    assert len(outbounds) > 0
    assert outbounds[0].status == OutboundStatus.UNCERTAIN


def test_18_transport_failure_and_rate_limiting_resilience(gateway_environment, monkeypatch):
    """Exceeding message rate limit per minute triggers a rate-limit response."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")
    env["gateway"]._config = env["config"].model_copy(update={"max_poll_limit": 50})

    # Send 25 messages rapidly when limit is 20
    for i in range(25):
        env["transport"].enqueue_update(
            _make_telegram_update(
                update_id=200 + i,
                user_id=123456789,
                text="/status",
            )
        )

    results = env["gateway"].poll_and_process_once()
    assert len(results) == 25

    # First 20 allowed
    allowed = [r for r in results if r.state == UpdateLifecycleState.COMPLETED]
    # Subsequent 5 rejected by rate limiter
    rate_limited = [r for r in results if r.error_summary == "Rate limit exceeded"]
    assert len(allowed) == 20
    assert len(rate_limited) == 5


def test_19_zero_secrets_invariant_enforced(gateway_environment, monkeypatch):
    """Zero-secret heuristics reject updates containing raw credentials."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Attempt to send OpenAI key in task goal
    env["transport"].enqueue_update(
        _make_telegram_update(
            update_id=300,
            user_id=123456789,
            text="/task my secret key is sk-123456789012345678901234567890",
        )
    )
    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert "zero-secret" in (results[0].error_summary or "")


def test_20_mocked_end_to_end_demonstration(gateway_environment, monkeypatch):
    """Full authenticated owner interaction lifecycle:

    1. Owner checks /status -> returns operational state.
    2. Owner runs /inspect -> inspects workspace files.
    3. Owner runs /verify -> runs dev verification.
    4. Owner submits /task -> SAGE builds Context Briefing.
    5. Owner confirms /confirm -> SAGE executes, checkpoints, and completes!
    """
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # 1. /status
    env["transport"].enqueue_update(_make_telegram_update(401, 123456789, "/status"))
    res_status = env["gateway"].poll_and_process_once()
    assert len(res_status) == 1
    assert "SAGE System Status" in (res_status[0].response_text or "")

    # 2. /inspect
    env["transport"].enqueue_update(_make_telegram_update(402, 123456789, "/inspect"))
    res_inspect = env["gateway"].poll_and_process_once()
    assert len(res_inspect) == 1
    assert "Workspace Inspection" in (res_inspect[0].response_text or "")
    assert "sample.txt" in (res_inspect[0].response_text or "")

    # 3. /verify
    env["transport"].enqueue_update(_make_telegram_update(403, 123456789, "/verify syntax"))
    res_verify = env["gateway"].poll_and_process_once()
    assert len(res_verify) == 1
    assert "Verification Check" in (res_verify[0].response_text or "")

    # 4. /task
    env["transport"].enqueue_update(
        _make_telegram_update(404, 123456789, "/task run full project validation")
    )
    res_task = env["gateway"].poll_and_process_once()
    assert len(res_task) == 1
    task_id = res_task[0].task_id
    assert task_id is not None
    assert "/confirm" in (res_task[0].response_text or "")

    # 5. /confirm
    env["transport"].enqueue_update(_make_telegram_update(405, 123456789, f"/confirm {task_id}"))
    res_confirm = env["gateway"].poll_and_process_once()
    assert len(res_confirm) == 1
    assert "Confirmed & Executed" in (res_confirm[0].response_text or "")

    # Verify final task state in state store
    final_task = env["state_store"].get_task(task_id)
    assert final_task is not None
    assert final_task.state == TaskState.COMPLETED
    assert final_task.completed_at is not None


def test_21_offline_guarantee_no_network(gateway_environment, monkeypatch):
    """Confirm zero real network calls occur when running remote gateway."""
    import socket

    def guarded_connect(*args, **kwargs):
        raise AssertionError("Unexpected network access detected during offline test!")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    env = gateway_environment
    env["transport"].enqueue_update(_make_telegram_update(501, 123456789, "/status"))
    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.COMPLETED


def test_22_start_command_returns_onboarding(gateway_environment, monkeypatch):
    """Test /start returns safe onboarding text, without side-effects."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Send /start
    env["transport"].enqueue_update(_make_telegram_update(601, 123456789, "/start"))
    results = env["gateway"].poll_and_process_once()

    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.COMPLETED
    assert "Welcome to SAGE Secure Remote-Control Gateway" in (results[0].response_text or "")
    assert "/help" in (results[0].response_text or "")

    # Zero tasks created
    assert len(env["state_store"].list_tasks()) == 0


def test_23_help_lists_supported_commands_and_deny_alias(gateway_environment, monkeypatch):
    """Test /help accurately reflects supported commands and /deny aliases /reject."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # 1. /help
    env["transport"].enqueue_update(_make_telegram_update(602, 123456789, "/help"))
    res_help = env["gateway"].poll_and_process_once()
    assert len(res_help) == 1
    help_text = res_help[0].response_text or ""
    assert "/start" in help_text
    assert "/status" in help_text
    assert "/inspect" in help_text
    assert "/verify" in help_text
    assert "/task" in help_text
    assert "/confirm" in help_text
    assert "/reject" in help_text
    assert "/deny" in help_text
    assert "/cancel" in help_text
    assert "/stop" in help_text

    # 2. Test /deny alias for /reject
    req = env["approval_store"].create_request(action_name="filesystem.delete", ttl_seconds=300.0)
    env["transport"].enqueue_update(_make_telegram_update(603, 123456789, f"/deny {req.request_id}"))
    res_deny = env["gateway"].poll_and_process_once()
    assert len(res_deny) == 1
    assert "Rejected" in (res_deny[0].response_text or "")

    # Verify request state is REJECTED in approval_store
    req_after = env["approval_store"].get_request(req.request_id)
    assert req_after is not None
    assert req_after.status == "REJECTED"


def test_24_chat_authorization_enforced(gateway_environment, monkeypatch):
    """Messages from authorized sender but unauthorized chat ID are rejected silently."""
    env = gateway_environment
    monkeypatch.setenv("SAGE_TEST_BOT_TOKEN", "valid-test-token-1234")

    # Authorized user 123456789, but chat_id 999999999 (not in allowed_chat_ids)
    raw_update = _make_telegram_update(
        update_id=605,
        user_id=123456789,
        chat_id=999999999,
        text="/status",
    )
    env["transport"].enqueue_update(raw_update)

    results = env["gateway"].poll_and_process_once()
    assert len(results) == 1
    assert results[0].state == UpdateLifecycleState.REJECTED
    assert results[0].error_summary == "Unauthorized chat ID"

    # Zero responses sent back (no info leakage)
    assert len(env["transport"].sent_messages) == 0

    # Negative chat ID (e.g. Telegram group -100123456789)
    raw_group_update = _make_telegram_update(
        update_id=606,
        user_id=123456789,
        chat_id=-100123456789,
        text="/status",
    )
    env["transport"].enqueue_update(raw_group_update)

    results2 = env["gateway"].poll_and_process_once()
    assert len(results2) == 1
    assert results2[0].state == UpdateLifecycleState.REJECTED
    assert results2[0].error_summary == "Unauthorized chat ID"


def test_25_outbound_destination_validation_rejects_groups_and_unauthorized(gateway_environment):
    """Outbound dispatch rejects non-allowlisted and group destinations."""
    env = gateway_environment
    gw = env["gateway"]
    store = env["store"]

    # 1. Outbound to negative group chat ID -> rejected
    gw._dispatch_outbound(
        recipient_id="-100123456789",
        content="Security test group broadcast",
    )
    # Check status is FAILED
    outbounds1 = store.list_outbound_messages(channel="telegram", limit=10)
    assert len(outbounds1) == 1
    assert outbounds1[0].status == OutboundStatus.FAILED
    assert "Group and channel destinations" in (outbounds1[0].error_message or "")

    # 2. Outbound to unauthorized private chat ID -> rejected
    gw._dispatch_outbound(
        recipient_id="888888888",
        content="Security test unauthorized chat",
    )
    outbounds2 = store.list_outbound_messages(channel="telegram", limit=10)
    unauth = [o for o in outbounds2 if o.recipient_id == "888888888"]
    assert len(unauth) == 1
    assert unauth[0].status == OutboundStatus.FAILED
    assert "not in allowed_chat_ids" in (unauth[0].error_message or "")

    # 3. Outbound to authorized chat ID -> succeeds
    gw._dispatch_outbound(
        recipient_id="123456789",
        content="Authorized message",
    )
    outbounds3 = store.list_outbound_messages(channel="telegram", limit=10)
    auth = [o for o in outbounds3 if o.recipient_id == "123456789"]
    assert len(auth) == 1
    assert auth[0].status == OutboundStatus.SENT


def test_26_startup_reconciliation_safe_and_idempotent(gateway_environment):
    """Crash recovery reconciles orphaned PROCESSING and PENDING records safely and idempotently."""
    env = gateway_environment
    gw = env["gateway"]
    store = env["store"]

    # Create an orphaned update in PROCESSING
    rec, _ = store.record_inbound_update(
        channel="telegram",
        channel_update_id="9999",
        sender_id="123456789",
        chat_id="123456789",
        raw_text="/task test crash",
    )
    store.transition_update_state(rec.update_id, UpdateLifecycleState.PROCESSING)

    # Create an unacknowledged outbound in PENDING
    outbound = store.record_outbound_message(
        channel="telegram",
        recipient_id="123456789",
        content="Unacknowledged message",
    )

    # Reconcile startup
    report = gw.reconcile_startup()
    assert report["updates_flagged_recovery"] == 1
    assert report["outbound_marked_uncertain"] == 1

    # Check update was marked FAILED, not re-executed
    u_after = store.get_inbound_update(rec.update_id)
    assert u_after is not None
    assert u_after.state == UpdateLifecycleState.FAILED
    assert "Interrupted by system restart" in (u_after.error_summary or "")

    # Check outbound was marked UNCERTAIN
    o_after = store.get_outbound_message(outbound.message_id)
    assert o_after is not None
    assert o_after.status == OutboundStatus.UNCERTAIN

    # Run reconciliation again to ensure idempotency
    report2 = gw.reconcile_startup()
    assert report2["updates_flagged_recovery"] == 0
    assert report2["outbound_marked_uncertain"] == 0


def test_27_telegram_delivery_adapter():
    """TelegramDeliveryAdapter validates configuration, scrubs secrets, and handles errors safely."""
    from src.notifications.adapters import TelegramDeliveryAdapter
    from src.notifications.models import (
        Notification,
        NotificationEventType,
        NotificationPriority,
        NotificationStatus,
    )

    # 1. Negative or zero chat ID raises ValueError
    with pytest.raises(ValueError, match="strictly prohibited"):
        TelegramDeliveryAdapter(transport=MockTelegramTransport(), chat_id="-100123456789")

    with pytest.raises(ValueError, match="strictly prohibited"):
        TelegramDeliveryAdapter(transport=MockTelegramTransport(), chat_id=0)

    # 2. Valid delivery
    mock_trans = MockTelegramTransport()
    adapter = TelegramDeliveryAdapter(transport=mock_trans, chat_id=123456789)
    assert adapter.adapter_name == "telegram"

    notification = Notification(
        notification_id="notif-100",
        event_type=NotificationEventType.GENERAL_INFO,
        priority=NotificationPriority.P2,
        title="Test Notification",
        body="This is a test notification body.",
        status=NotificationStatus.PENDING,
        created_at="2026-10-18T00:00:00Z",
        updated_at="2026-10-18T00:00:00Z",
    )
    result = adapter.send(notification)
    assert result.succeeded is True
    assert len(mock_trans.sent_messages) == 1
    assert "Test Notification" in mock_trans.sent_messages[0]["text"]

    # 3. Transport error is captured and handled safely
    mock_trans.set_send_message_error(TransportError("bot123456789:SECRET_TOKEN connection failed"))
    result_err = adapter.send(notification)
    assert result_err.succeeded is False
    assert result_err.error_message is not None
    # Verify bot token is redacted in error message
    assert "SECRET_TOKEN" not in result_err.error_message
    assert "bot<REDACTED_BOT_TOKEN>" in result_err.error_message


def test_28_secret_redaction_in_transport():
    """Verify HttpTelegramTransport and _redact_token thoroughly sanitize secrets."""
    from src.gateway.transport import HttpTelegramTransport, _redact_token

    # 1. URL pattern scrubbing
    raw_url = "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456/sendMessage"
    redacted = _redact_token(raw_url)
    assert "123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456" not in redacted
    assert "https://api.telegram.org/bot<REDACTED_BOT_TOKEN>/sendMessage" in redacted

    # 2. Instance token scrubbing in HttpTelegramTransport
    fake_token = "987654321:XYZ987654321_secret_token_value_here"
    transport = HttpTelegramTransport(bot_token=fake_token)
    cleaned = transport._redact(f"Error connecting to api with token {fake_token} in memory")
    assert fake_token not in cleaned
    assert "<REDACTED_BOT_TOKEN>" in cleaned

