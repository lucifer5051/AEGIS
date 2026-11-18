"""PROMPT 062: Gateway TrustCore Enforcement Audit & Verification Suite.

Tests Cases A through G verifying that remote gateway handlers cannot bypass
TrustCore capability checks, autonomy policies, approval gates, or identity tracking.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from src.config.models import TelegramConfig
from src.gateway.gateway import ChannelGateway
from src.gateway.models import RouterResponse, UpdateLifecycleState
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import CommandRouter
from src.gateway.store import GatewayStore
from src.gateway.transport import MockTelegramTransport
from src.manager.models import StepPlan, StepState, TaskState
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.skills.first_party import register_first_party_skills
from src.skills.registry import SkillRegistry
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.approval import ApprovalStore
from src.trust.audit import AuditDecision, AuditEventType
from src.trust.policy import AutonomyTier


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    """Isolated runtime database in temporary directory outside repository."""
    db_file = tmp_path / "test_gateway_trustcore.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Isolated sandbox directory for tool execution."""
    sb = tmp_path / "workspace"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "test_file.txt").write_text("Test content", encoding="utf-8")
    return sb


def _build_env(
    test_db: sqlite3.Connection,
    sandbox: Path,
    tier: AutonomyTier = AutonomyTier.FULL_BOUNDED,
    hard_denied: frozenset | None = None,
) -> Dict[str, Any]:
    """Helper to build a configured SAGE environment with customizable autonomy tier."""
    authority = build_trust_authority(
        test_db,
        autonomy_tier=tier,
    )
    if hard_denied is not None:
        authority._policy._hard_denied = hard_denied
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
    transport = MockTelegramTransport()
    router = CommandRouter(
        supervisor=supervisor,
        tool_broker=broker,
        trust_authority=authority,
        approval_store=approval_store,
        workspace_dir=sandbox,
    )
    config = TelegramConfig(
        enabled=True,
        bot_token_env="SAGE_TEST_BOT_TOKEN",
        allowed_user_ids=[123456789],
        allowed_chat_ids=[123456789],
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
        "db": test_db,
        "authority": authority,
        "broker": broker,
        "supervisor": supervisor,
        "router": router,
        "gateway": gateway,
        "transport": transport,
        "store": gateway_store,
        "approval_store": approval_store,
        "sandbox": sandbox,
        "config": config,
    }


# ==============================================================================
# CASE A: UNAUTHORIZED TOOL REQUEST
# ==============================================================================

def test_case_a_hard_denied_tool_request_fails_closed(test_db: sqlite3.Connection, sandbox: Path):
    """Confirm that an action hard-denied by policy cannot execute through remote commands."""
    # Hard-deny process.run_test so that /verify is categorically blocked
    env = _build_env(
        test_db,
        sandbox,
        tier=AutonomyTier.FULL_BOUNDED,
        hard_denied=frozenset({"process.run_test", "policy.change"}),
    )
    router: CommandRouter = env["router"]

    resp = router.route(
        "/verify test",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )

    # Must report failure; exit code non-zero, verification_passed is False
    assert "Verification Check: `test`" in resp.response_text
    assert "Passed: False" in resp.response_text or "Passed:* False" in resp.response_text


def test_case_a_task_with_unknown_tool_fails_closed(test_db: sqlite3.Connection, sandbox: Path):
    """Confirm that a task proposing an unregistered tool is denied during /confirm."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.FULL_BOUNDED)
    supervisor: Supervisor = env["supervisor"]
    router: CommandRouter = env["router"]

    # Submit a task with an invalid/unregistered tool
    task = supervisor.submit_goal("Run arbitrary tool")
    step = StepPlan(
        action_name="system.arbitrary",
        tool_name="unregistered_tool_xyz",
        is_idempotent=True,
        arguments={},
    )
    supervisor.build_context_briefing(task.task_id, proposed_steps=[step])

    # Attempt to confirm via gateway
    resp = router.route(
        f"/confirm {task.task_id}",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )

    assert resp.completed is True
    # The task outcome state must be FAILED because tool does not exist
    stored_task = supervisor._store.get_task(task.task_id)
    assert stored_task.state == TaskState.FAILED
    assert "ToolNotFoundError" in (stored_task.error_summary or "")


# ==============================================================================
# CASE B: RESTRICTED AUTONOMY TIER (STRICT_MANUAL)
# ==============================================================================

def test_case_b_strict_manual_blocks_autonomous_verification(test_db: sqlite3.Connection, sandbox: Path):
    """Under STRICT_MANUAL, medium-risk actions like /verify require approval and fail closed."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.STRICT_MANUAL)
    router: CommandRouter = env["router"]

    resp = router.route(
        "/verify test",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )

    # process.run_test is MEDIUM risk -> STRICT_MANUAL escalates to ApprovalRequiredError
    assert "Passed:* False" in resp.response_text or "Passed: False" in resp.response_text
    assert "ApprovalRequiredError" in resp.response_text or "Verification failed" in resp.response_text


def test_case_b_strict_manual_task_execution_escalates_to_awaiting_approval(
    test_db: sqlite3.Connection, sandbox: Path
):
    """Under STRICT_MANUAL, confirming a task with medium/high risk actions enters AWAITING_APPROVAL."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.STRICT_MANUAL)
    supervisor: Supervisor = env["supervisor"]
    router: CommandRouter = env["router"]

    # Propose a task that uses dev_runner (MEDIUM risk)
    resp_task = router.route(
        "/task verify development checks",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    task_id = resp_task.task_id
    assert task_id is not None

    # Confirm the task
    resp_confirm = router.route(
        f"/confirm {task_id}",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )

    # Must halt execution in AWAITING_APPROVAL
    assert resp_confirm.awaiting_approval is True
    assert resp_confirm.completed is False

    stored_task = supervisor._store.get_task(task_id)
    assert stored_task.state == TaskState.AWAITING_APPROVAL

    steps = supervisor._store.list_task_steps(task_id)
    assert any(s.state == StepState.AWAITING_APPROVAL for s in steps)


# ==============================================================================
# CASE C: FORGED OR MISSING IDENTITY
# ==============================================================================

def test_case_c_missing_or_empty_sender_id_fails_closed(test_db: sqlite3.Connection, sandbox: Path):
    """Submitting commands with empty or whitespace sender_id must fail closed."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.FULL_BOUNDED)
    router: CommandRouter = env["router"]

    for bad_sender in ("", "   ", None):
        resp = router.route(
            "/inspect",
            sender_id=bad_sender,  # type: ignore[arg-type]
            chat_id="123456789",
        )
        assert "Authentication failed" in resp.response_text or "missing or empty sender identity" in resp.response_text


def test_case_c_sender_identity_propagated_to_trust_authority_audit(
    test_db: sqlite3.Connection, sandbox: Path
):
    """Authenticated caller identity must be propagated to Trust Core audit logs (not generic 'telegram')."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.FULL_BOUNDED)
    router: CommandRouter = env["router"]

    # 1. Inspect with specific Telegram sender
    router.route(
        "/inspect",
        sender_id="99887766",
        chat_id="123456789",
        channel="telegram",
    )

    # Query audit logs for recent decisions
    # The ALLOW path writes CAPABILITY_ISSUED where action_name is in telemetry_json
    cursor = test_db.execute(
        "SELECT actor, telemetry_json FROM audit_records WHERE event_type = 'CAPABILITY_ISSUED' ORDER BY rowid DESC LIMIT 10;"
    )
    rows = cursor.fetchall()
    assert rows, "Expected at least one CAPABILITY_ISSUED audit record"
    # Find the row for filesystem.list action
    filesystem_list_row = None
    for row in rows:
        if '"filesystem.list"' in (row[1] or ""):
            filesystem_list_row = row
            break
    assert filesystem_list_row is not None, f"No audit record found for filesystem.list. Got: {rows}"
    assert filesystem_list_row[0] == "telegram:99887766", (
        f"Expected actor 'telegram:99887766', got '{filesystem_list_row[0]}'"
    )

    # 2. Inspect with WhatsApp sender
    router.route(
        "/inspect",
        sender_id="44712345678@s.whatsapp.net",
        chat_id="44712345678@s.whatsapp.net",
        channel="whatsapp",
    )

    cursor = test_db.execute(
        "SELECT actor, telemetry_json FROM audit_records WHERE event_type = 'CAPABILITY_ISSUED' ORDER BY rowid DESC LIMIT 10;"
    )
    rows = cursor.fetchall()
    filesystem_list_row2 = None
    for row in rows:
        if '"filesystem.list"' in (row[1] or "") and row[0].startswith("whatsapp:"):
            filesystem_list_row2 = row
            break
    assert filesystem_list_row2 is not None, f"No WhatsApp filesystem.list audit record found. Got: {rows}"
    assert filesystem_list_row2[0] == "whatsapp:44712345678@s.whatsapp.net", (
        f"Expected actor 'whatsapp:44712345678@s.whatsapp.net', got '{filesystem_list_row2[0]}'"
    )


# ==============================================================================
# CASE D: DIRECT EXECUTOR PATH GOES THROUGH TRUSTCORE
# ==============================================================================

def test_case_d_confirm_always_enforces_trustcore(test_db: sqlite3.Connection, sandbox: Path):
    """Confirming a task routes through Supervisor.execute_task and invokes TrustAuthority."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.FULL_BOUNDED)
    router: CommandRouter = env["router"]
    authority = env["authority"]

    resp_task = router.route(
        "/task inspect files in workspace",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    task_id = resp_task.task_id

    # Spy on request_capability to ensure TrustAuthority is invoked
    orig_req_cap = authority.request_capability
    cap_calls: List[str] = []

    def spy_request_capability(*args, **kwargs):
        action = kwargs.get("action_name", args[0] if args else "")
        cap_calls.append(action)
        return orig_req_cap(*args, **kwargs)

    with patch.object(authority, "request_capability", side_effect=spy_request_capability):
        resp_confirm = router.route(
            f"/confirm {task_id}",
            sender_id="123456789",
            chat_id="123456789",
            channel="telegram",
        )

    assert resp_confirm.completed is True
    # Verify that TrustAuthority was actively queried
    assert "filesystem.list" in cap_calls


# ==============================================================================
# CASE E: REPLANNING THROUGH THE GATEWAY
# ==============================================================================

def test_case_e_replanned_steps_enforce_trustcore_authorization(
    test_db: sqlite3.Connection, sandbox: Path
):
    """When a task undergoes replanning or multi-step execution, all newly planned steps enforce TrustCore."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.STRICT_MANUAL)
    supervisor: Supervisor = env["supervisor"]
    router: CommandRouter = env["router"]

    task = supervisor.submit_goal("Goal with replanned steps")

    # Initial plan has low-risk step (read)
    step1 = StepPlan(
        action_name="filesystem.list",
        tool_name="file_list",
        is_idempotent=True,
        arguments={"directory": str(sandbox)},
    )
    # Replanned / subsequent step has medium-risk step (test runner)
    step2 = StepPlan(
        action_name="process.run_test",
        tool_name="dev_runner",
        is_idempotent=True,
        arguments={"verification_type": "syntax", "code": "print('ok')"},
    )
    supervisor.build_context_briefing(task.task_id, proposed_steps=[step1, step2])

    # Execute via gateway /confirm
    resp = router.route(
        f"/confirm {task.task_id}",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )

    # Step 1 should pass, Step 2 should be blocked by STRICT_MANUAL
    steps = supervisor._store.list_task_steps(task.task_id)
    assert len(steps) == 2
    assert steps[0].state == StepState.COMPLETED
    assert steps[1].state == StepState.AWAITING_APPROVAL
    assert resp.awaiting_approval is True


# ==============================================================================
# CASE F: APPROVAL INTEGRITY
# ==============================================================================

def test_case_f_approval_integrity_fails_closed(test_db: sqlite3.Connection, sandbox: Path):
    """Bypassing approval via flags, fake IDs, or invalid state transitions fails closed."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.STRICT_MANUAL)
    router: CommandRouter = env["router"]
    approval_store: ApprovalStore = env["approval_store"]

    # 1. Fake approval request ID
    resp_fake = router.route(
        "/approve non-existent-req-id-12345",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    assert "Failed to approve request" in resp_fake.response_text or "not found" in resp_fake.response_text

    # 2. Expired approval request
    import time
    expired_rec = approval_store.create_request(
        task_id="task_123",
        action_name="process.run_test",
        action_risk_level="MEDIUM",
        ttl_seconds=0.01,
    )
    time.sleep(0.05)  # Wait for expiration

    resp_expired = router.route(
        f"/approve {expired_rec.request_id}",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    assert "Failed to approve request" in resp_expired.response_text or "non-pending status 'EXPIRED'" in resp_expired.response_text

    # 3. Supplying extra flags to /confirm does not bypass TrustCore
    resp_task = router.route(
        "/task verify dev",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    t_id = resp_task.task_id
    assert t_id is not None

    resp_bypass = router.route(
        f"/confirm {t_id} --approved --force-execution",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    # Either fails to parse task ID or executes under strict TrustCore (which halts in AWAITING_APPROVAL)
    if resp_bypass.task_id:
        assert resp_bypass.awaiting_approval is True
    else:
        assert "Please provide a valid task ID" in resp_bypass.response_text


# ==============================================================================
# CASE G: VALID AUTHORIZED REQUEST
# ==============================================================================

def test_case_g_valid_authorized_request_succeeds(test_db: sqlite3.Connection, sandbox: Path):
    """Legitimately authorized requests execute cleanly and return proper evidence."""
    env = _build_env(test_db, sandbox, tier=AutonomyTier.FULL_BOUNDED)
    router: CommandRouter = env["router"]

    # 1. /inspect
    resp_inspect = router.route(
        "/inspect",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    assert "Workspace Inspection (Read-Only)" in resp_inspect.response_text
    assert "test_file.txt" in resp_inspect.response_text

    # 2. /task -> /confirm
    resp_task = router.route(
        "/task inspect directory structure",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    assert resp_task.awaiting_approval is True
    assert resp_task.task_id is not None

    resp_confirm = router.route(
        f"/confirm {resp_task.task_id}",
        sender_id="123456789",
        chat_id="123456789",
        channel="telegram",
    )
    assert resp_confirm.completed is True
    assert resp_confirm.awaiting_approval is False
    assert "Outcome State:* `COMPLETED`" in resp_confirm.response_text
