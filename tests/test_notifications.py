"""SAGE Notification Subsystem -- Comprehensive Offline Test Suite.

Prompt: 028
Scope:
- Persistence (SQLite store CRUD, delivery attempts)
- Priority levels and quiet hours enforcement
- Deduplication key suppression
- Bounded delivery retry and FAILED terminal state
- Delivery adapter failures (never corrupt notification state)
- Restart recovery (store state survives reconnect)
- Acknowledgement and dismissal (advisory only)
- Approval escalation integration (notifications NEVER grant approvals)
- Expired approvals remain invalid regardless of notification state
- Secret redaction (zero-secret invariant in all persisted fields)
- Integration with scheduler FAIL_ALERT (missed-run notifications)
- Integration with workflow events (completed, failed, paused)
- NotificationManager.retry_pending_deliveries (bounded retry loop)

Constraints:
- 100% offline, zero network calls.
- All tests use in-memory or tmp SQLite databases.
- Notification failures MUST NOT corrupt workflow or task state.
- Notifications MUST NOT approve actions or bypass Trust Core.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.config.models import NotificationConfig
from src.notifications import (
    DeliveryAdapter,
    DeliveryResult,
    LocalLogAdapter,
    Notification,
    NotificationAlreadyAcknowledgedError,
    NotificationEventType,
    NotificationManager,
    NotificationNotFoundError,
    NotificationPriority,
    NotificationSecretLeakError,
    NotificationStatus,
    NotificationStore,
    build_default_adapters,
)
from src.storage.database import initialize_runtime_database
from src.trust.errors import SecretLeakError

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ==============================================================================
# FIXTURES
# ==============================================================================


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    """Initialized in-memory SQLite database with all SAGE migrations applied."""
    db_file = tmp_path / "test_notifications.db"
    return initialize_runtime_database(db_file, migrations_dir=MIGRATIONS_DIR)


@pytest.fixture
def store(db: sqlite3.Connection) -> NotificationStore:
    return NotificationStore(db)


@pytest.fixture
def default_config() -> NotificationConfig:
    return NotificationConfig()


@pytest.fixture
def manager(store: NotificationStore, default_config: NotificationConfig) -> NotificationManager:
    """NotificationManager with LocalLogAdapter and default config."""
    adapters = build_default_adapters()
    return NotificationManager(store=store, config=default_config, adapters=adapters)


# ==============================================================================
# HELPER: ALWAYS-FAILING ADAPTER
# ==============================================================================


class AlwaysFailAdapter(DeliveryAdapter):
    """Adapter that always returns failure without raising."""

    @property
    def adapter_name(self) -> str:
        return "always_fail"

    def send(self, notification: Notification) -> DeliveryResult:
        return DeliveryResult(
            succeeded=False,
            adapter=self.adapter_name,
            error_message="Simulated adapter failure.",
        )


class AlwaysRaiseAdapter(DeliveryAdapter):
    """Adapter that raises instead of returning DeliveryResult (contract violation)."""

    @property
    def adapter_name(self) -> str:
        return "always_raise"

    def send(self, notification: Notification) -> DeliveryResult:
        raise RuntimeError("Unexpected error from adapter!")


# ==============================================================================
# TEST 01: PERSISTENCE — CREATE, GET, LIST
# ==============================================================================


def test_01_notification_persistence_create_and_retrieve(store: NotificationStore) -> None:
    """Notifications survive write-read cycles in SQLite."""
    nid = "notif-001"
    n = store.create_notification(
        notification_id=nid,
        event_type=NotificationEventType.WORKFLOW_COMPLETED,
        priority=NotificationPriority.P2,
        title="Workflow done",
        body="My workflow completed successfully.",
        source_type="workflow",
        source_id="run-abc",
        project_id="proj-x",
        dedup_key="wf_completed:run-abc",
    )
    assert n.notification_id == nid
    assert n.status == NotificationStatus.PENDING
    assert n.delivery_attempts == 0

    fetched = store.get_notification(nid)
    assert fetched is not None
    assert fetched.event_type == NotificationEventType.WORKFLOW_COMPLETED
    assert fetched.title == "Workflow done"
    assert fetched.dedup_key == "wf_completed:run-abc"

    # List returns it
    listed = store.list_notifications(status=NotificationStatus.PENDING)
    assert any(n.notification_id == nid for n in listed)


# ==============================================================================
# TEST 02: DELIVERY SUCCESS → STATUS BECOMES DELIVERED
# ==============================================================================


def test_02_successful_delivery_marks_notification_delivered(
    manager: NotificationManager,
    store: NotificationStore,
) -> None:
    """When LocalLogAdapter succeeds, notification status becomes DELIVERED."""
    n = manager.notify(
        event_type=NotificationEventType.GENERAL_INFO,
        title="Info notification",
        body="This is a test info notification.",
        priority=NotificationPriority.P3,
    )
    assert n.status == NotificationStatus.DELIVERED
    assert n.delivery_attempts == 1

    fetched = store.get_notification(n.notification_id)
    assert fetched is not None
    assert fetched.status == NotificationStatus.DELIVERED


# ==============================================================================
# TEST 03: DELIVERY FAILURE → BOUNDED RETRIES → FAILED STATUS
# ==============================================================================


def test_03_delivery_failure_bounded_retries_and_failed_status(
    store: NotificationStore,
    default_config: NotificationConfig,
) -> None:
    """When all adapters fail, notification is PENDING then eventually FAILED."""
    failing_manager = NotificationManager(
        store=store,
        config=default_config,
        adapters=[AlwaysFailAdapter()],
    )

    # Create with max_delivery_attempts=2
    n = failing_manager.notify(
        event_type=NotificationEventType.WORKFLOW_FAILED,
        title="Workflow error",
        body="The workflow encountered an unrecoverable error.",
        priority=NotificationPriority.P1,
        max_delivery_attempts=2,
    )
    # First attempt failed; since max_attempts=2, attempts=1 < 2, still PENDING
    assert n.status == NotificationStatus.PENDING
    assert n.delivery_attempts == 1

    # retry_pending_deliveries: attempt 2 → still fails → FAILED (1 < 2 now becomes 2 >= 2)
    results = failing_manager.retry_pending_deliveries()
    final = next(r for r in results if r.notification_id == n.notification_id)
    assert final.status == NotificationStatus.FAILED
    assert final.delivery_attempts == 2

    # Confirm persisted
    fetched = store.get_notification(n.notification_id)
    assert fetched is not None
    assert fetched.status == NotificationStatus.FAILED


# ==============================================================================
# TEST 04: DELIVERY FAILURE DOES NOT CORRUPT NOTIFICATION STATE
# ==============================================================================


def test_04_delivery_failure_never_corrupts_store_state(
    store: NotificationStore,
    default_config: NotificationConfig,
) -> None:
    """A delivery adapter that raises must not leave the notification in a corrupt state."""
    raise_manager = NotificationManager(
        store=store,
        config=default_config,
        adapters=[AlwaysRaiseAdapter()],
    )

    n = raise_manager.notify(
        event_type=NotificationEventType.GENERAL_INFO,
        title="Test crash adapter",
        body="This notification hits a crashing adapter.",
        max_delivery_attempts=1,
    )
    # Adapter raised, but manager caught it. With max_attempts=1 and attempts=1 → FAILED
    assert n.status == NotificationStatus.FAILED

    # Store is not corrupted: we can still read, write, and query
    fetched = store.get_notification(n.notification_id)
    assert fetched is not None
    assert fetched.status == NotificationStatus.FAILED

    # New notifications can still be created
    n2 = store.create_notification(
        notification_id="extra-ok",
        event_type=NotificationEventType.GENERAL_INFO,
        priority=NotificationPriority.P3,
        title="Post-crash notification",
        body="Store is still functional.",
    )
    assert n2.notification_id == "extra-ok"


# ==============================================================================
# TEST 05: DEDUPLICATION — DUPLICATE KEY IS SUPPRESSED
# ==============================================================================


def test_05_deduplication_suppresses_duplicate_notifications(
    manager: NotificationManager,
    store: NotificationStore,
) -> None:
    """Notifications with the same dedup_key are suppressed while original is active."""
    dedup = "approval_required:req-123"

    n1 = manager.notify(
        event_type=NotificationEventType.APPROVAL_REQUIRED,
        title="Approval Required",
        body="Action X requires approval.",
        dedup_key=dedup,
    )
    assert n1.status == NotificationStatus.DELIVERED

    # Second notification with same dedup_key → SUPPRESSED
    n2 = manager.notify(
        event_type=NotificationEventType.APPROVAL_REQUIRED,
        title="Approval Required",
        body="Action X requires approval.",
        dedup_key=dedup,
    )
    assert n2.status == NotificationStatus.SUPPRESSED

    # Only the first was delivered
    delivered = store.list_notifications(status=NotificationStatus.DELIVERED)
    assert len([n for n in delivered if n.dedup_key == dedup]) == 1


# ==============================================================================
# TEST 06: QUIET HOURS SUPPRESS LOW-PRIORITY NOTIFICATIONS
# ==============================================================================


def test_06_quiet_hours_suppresses_low_priority_notifications(
    store: NotificationStore,
) -> None:
    """During quiet hours, P2-P4 notifications are suppressed; P0/P1 bypass."""
    quiet_config = NotificationConfig(
        quiet_hours_enabled=True,
        quiet_hours_start_utc="00:00",
        quiet_hours_end_utc="23:59",  # Always quiet
    )

    quiet_manager = NotificationManager(
        store=store, config=quiet_config, adapters=[LocalLogAdapter()]
    )

    # P3 notification → SUPPRESSED during quiet hours
    n_low = quiet_manager.notify(
        event_type=NotificationEventType.GENERAL_INFO,
        title="Low priority test",
        body="Should be suppressed in quiet hours.",
        priority=NotificationPriority.P3,
    )
    assert n_low.status == NotificationStatus.SUPPRESSED

    # P0 notification → bypasses quiet hours, DELIVERED
    n_critical = quiet_manager.notify(
        event_type=NotificationEventType.RECOVERY_EXHAUSTED,
        title="Critical alert",
        body="Immediate user attention required.",
        priority=NotificationPriority.P0,
    )
    assert n_critical.status == NotificationStatus.DELIVERED

    # P1 also bypasses
    n_high = quiet_manager.notify(
        event_type=NotificationEventType.APPROVAL_REQUIRED,
        title="Approval needed",
        body="This bypasses quiet hours.",
        priority=NotificationPriority.P1,
    )
    assert n_high.status == NotificationStatus.DELIVERED


# ==============================================================================
# TEST 07: ACKNOWLEDGEMENT AND DISMISSAL (ADVISORY ONLY)
# ==============================================================================


def test_07_acknowledgement_and_dismissal_are_advisory_only(
    manager: NotificationManager,
    store: NotificationStore,
) -> None:
    """Acknowledge and dismiss are purely informational -- never affect approval state."""
    n = manager.notify(
        event_type=NotificationEventType.WORKFLOW_COMPLETED,
        title="Workflow done",
        body="Workflow finished successfully.",
    )

    # Acknowledge
    acked = manager.acknowledge(n.notification_id)
    assert acked.status == NotificationStatus.ACKNOWLEDGED
    assert acked.acknowledged_at is not None

    # Double-acknowledge raises gracefully
    with pytest.raises(NotificationAlreadyAcknowledgedError):
        manager.acknowledge(n.notification_id)

    # Dismiss a different one
    n2 = manager.notify(
        event_type=NotificationEventType.GENERAL_INFO,
        title="Another notification",
        body="To be dismissed.",
    )
    dismissed = manager.dismiss(n2.notification_id)
    assert dismissed.status == NotificationStatus.DISMISSED
    assert dismissed.dismissed_at is not None

    # Acknowledge on unknown ID raises NotificationNotFoundError
    with pytest.raises(NotificationNotFoundError):
        manager.acknowledge("nonexistent-id")


# ==============================================================================
# TEST 08: RESTART RECOVERY — STATE SURVIVES RECONNECT
# ==============================================================================


def test_08_restart_recovery_state_survives_db_reconnect(tmp_path: Path) -> None:
    """Notification state persists across connection close and reopen."""
    db_file = tmp_path / "restart_test.db"
    conn1 = initialize_runtime_database(db_file, migrations_dir=MIGRATIONS_DIR)
    store1 = NotificationStore(conn1)
    mgr1 = NotificationManager(store=store1, adapters=[AlwaysFailAdapter()])

    n = mgr1.notify(
        event_type=NotificationEventType.WORKFLOW_FAILED,
        title="Workflow failed",
        body="A workflow failed and we need to retry.",
        max_delivery_attempts=3,
    )
    nid = n.notification_id
    assert n.status == NotificationStatus.PENDING
    conn1.close()

    # Reopen: state must still be present
    conn2 = sqlite3.connect(str(db_file))
    conn2.row_factory = sqlite3.Row
    store2 = NotificationStore(conn2)
    mgr2 = NotificationManager(store=store2, adapters=[LocalLogAdapter()])

    fetched = store2.get_notification(nid)
    assert fetched is not None
    assert fetched.status == NotificationStatus.PENDING
    assert fetched.delivery_attempts == 1

    # Retry with working adapter → DELIVERED
    results = mgr2.retry_pending_deliveries()
    recovered = next(r for r in results if r.notification_id == nid)
    assert recovered.status == NotificationStatus.DELIVERED
    conn2.close()


# ==============================================================================
# TEST 09: SECRET REDACTION — ZERO-SECRET INVARIANT
# ==============================================================================


def test_09_secret_redaction_zero_secret_invariant(
    store: NotificationStore,
    manager: NotificationManager,
) -> None:
    """Notifications with secrets in title/body/metadata are rejected before persistence."""
    # Title with secret pattern
    with pytest.raises((NotificationSecretLeakError, SecretLeakError)):
        store.create_notification(
            notification_id="bad-1",
            event_type=NotificationEventType.GENERAL_INFO,
            priority=NotificationPriority.P2,
            title="Token: bearer eyJhbGciOiJIUzI1NiJ9.abc",
            body="This should be rejected.",
        )

    # Body with secret pattern
    with pytest.raises((NotificationSecretLeakError, SecretLeakError)):
        store.create_notification(
            notification_id="bad-2",
            event_type=NotificationEventType.GENERAL_INFO,
            priority=NotificationPriority.P2,
            title="Normal title",
            body="password: mysecretpassword123",
        )

    # Manager-level API calls also propagate secret leak errors on error_summary
    with pytest.raises((NotificationSecretLeakError, SecretLeakError, Exception)):
        manager.notify_workflow_failed(
            workflow_id="wf-x",
            run_id="run-y",
            error_summary="token = Bearer secret_abc_xyz_123",
        )

    # A clean notification succeeds
    clean = store.create_notification(
        notification_id="clean-ok",
        event_type=NotificationEventType.GENERAL_INFO,
        priority=NotificationPriority.P3,
        title="Normal safe notification",
        body="No sensitive data here.",
    )
    assert clean.notification_id == "clean-ok"


# ==============================================================================
# TEST 10: APPROVAL NOTIFICATION DOES NOT GRANT OR CHANGE APPROVAL STATE
# ==============================================================================


def test_10_approval_notification_never_grants_or_changes_approval_state(
    manager: NotificationManager,
    db: sqlite3.Connection,
) -> None:
    """An APPROVAL_REQUIRED notification is purely advisory and cannot approve anything."""
    from src.trust.approval import ApprovalStore
    from src.trust.audit import AuditWriter
    from src.trust.authority import TrustAuthority
    from src.trust.capability import CapabilityFactory, CapabilityRegistry, ResourceScope
    from src.trust.errors import ApprovalRequiredError
    from src.trust.policy import AutonomyTier, PolicyEngine

    # Set up full Trust Core
    approval_store = ApprovalStore(db)
    factory = CapabilityFactory()
    registry = CapabilityRegistry(factory)
    policy = PolicyEngine(autonomy_tier=AutonomyTier.STRICT_MANUAL)
    auditor = AuditWriter(db)
    trust_auth = TrustAuthority(
        policy=policy, registry=registry, auditor=auditor, approval_store=approval_store
    )

    # 1. Action requires approval
    scope = ResourceScope(filesystem_prefixes=frozenset(["/safe"]))
    with pytest.raises(ApprovalRequiredError) as exc_info:
        trust_auth.request_capability(
            action_name="filesystem.write",
            scope=scope,
            actor="test_agent",
        )
    request_id = exc_info.value.request_id

    # 2. Create APPROVAL_REQUIRED notification
    notification = manager.notify_approval_required(
        action_name="filesystem.write",
        request_id=request_id,
        actor="test_agent",
    )
    assert notification.status == NotificationStatus.DELIVERED
    assert notification.event_type == NotificationEventType.APPROVAL_REQUIRED

    # 3. Notification does NOT change approval state in the store
    pending_request = approval_store.get_request(request_id)
    assert pending_request is not None
    assert pending_request.status == "PENDING"  # Still PENDING — not approved

    # 4. Acknowledging the notification does NOT approve the action
    manager.acknowledge(notification.notification_id)
    still_pending = approval_store.get_request(request_id)
    assert still_pending is not None
    assert still_pending.status == "PENDING"  # Still PENDING after acknowledgement

    # 5. Dismissing the notification does NOT approve or reject the action
    n2 = manager.notify_approval_required(
        action_name="filesystem.write",
        request_id=request_id,
        actor="test_agent",
        # Different notification for dismiss test
    )
    manager.dismiss(n2.notification_id)
    still_pending2 = approval_store.get_request(request_id)
    assert still_pending2 is not None
    assert still_pending2.status == "PENDING"


# ==============================================================================
# TEST 11: EXPIRED APPROVAL REMAINS INVALID REGARDLESS OF NOTIFICATION STATE
# ==============================================================================


def test_11_expired_approval_remains_invalid_regardless_of_notification(
    manager: NotificationManager,
    db: sqlite3.Connection,
) -> None:
    """An expired approval request cannot be consumed even after a notification is delivered."""
    import time

    from src.trust.approval import ApprovalStore
    from src.trust.audit import AuditWriter
    from src.trust.authority import TrustAuthority
    from src.trust.capability import CapabilityFactory, CapabilityRegistry, ResourceScope
    from src.trust.errors import ApprovalRequiredError, AuthorizationError
    from src.trust.policy import AutonomyTier, PolicyEngine

    approval_store = ApprovalStore(db)
    factory = CapabilityFactory()
    registry = CapabilityRegistry(factory)
    policy = PolicyEngine(autonomy_tier=AutonomyTier.STRICT_MANUAL)
    auditor = AuditWriter(db)
    trust_auth = TrustAuthority(
        policy=policy, registry=registry, auditor=auditor, approval_store=approval_store
    )

    # Create an approval request with TTL=0 (expires immediately)
    req = approval_store.create_request(
        action_name="filesystem.write",
        ttl_seconds=0,  # Expire immediately
    )

    time.sleep(0.05)  # Let it expire
    expired_count = approval_store.expire_pending_requests()
    assert expired_count >= 1

    fetched = approval_store.get_request(req.request_id)
    assert fetched is not None
    assert fetched.status == "EXPIRED"

    # Deliver an APPROVAL_EXPIRED notification
    n = manager.notify_approval_expired(
        action_name="filesystem.write",
        request_id=req.request_id,
    )
    assert n.status == NotificationStatus.DELIVERED

    # Even after notification delivery, the approval is still EXPIRED
    still_expired = approval_store.get_request(req.request_id)
    assert still_expired is not None
    assert still_expired.status == "EXPIRED"

    # Cannot consume expired approval to get a capability
    scope = ResourceScope(filesystem_prefixes=frozenset(["/safe"]))
    with pytest.raises((AuthorizationError, ApprovalRequiredError)):
        trust_auth.request_capability(
            action_name="filesystem.write",
            scope=scope,
            actor="test_agent",
            approval_request_id=req.request_id,
        )


# ==============================================================================
# TEST 12: SCHEDULER FAIL_ALERT INTEGRATION
# ==============================================================================


def test_12_scheduler_fail_alert_triggers_notification(
    manager: NotificationManager,
) -> None:
    """notify_schedule_failed and notify_schedule_missed emit correct events."""
    n_failed = manager.notify_schedule_failed(
        schedule_id="sched-001",
        target_action="daily_backup",
        error_summary="Backup storage full.",
    )
    assert n_failed.status == NotificationStatus.DELIVERED
    assert n_failed.event_type == NotificationEventType.SCHEDULE_FAILED
    assert n_failed.priority == NotificationPriority.P1
    assert n_failed.source_id == "sched-001"

    n_missed = manager.notify_schedule_missed(
        schedule_id="sched-002",
        target_action="nightly_report",
        delay_seconds=3610.0,
    )
    assert n_missed.status == NotificationStatus.DELIVERED
    assert n_missed.event_type == NotificationEventType.SCHEDULE_MISSED
    assert "3610" in n_missed.body or "3610.0" in n_missed.body or "overdue" in n_missed.body


# ==============================================================================
# TEST 13: WORKFLOW EVENT INTEGRATION
# ==============================================================================


def test_13_workflow_event_notifications(manager: NotificationManager) -> None:
    """Workflow completed, failed, and paused produce typed notifications."""
    n_comp = manager.notify_workflow_completed(
        workflow_id="wf-alpha",
        run_id="run-001",
        project_id="proj-p",
    )
    assert n_comp.status == NotificationStatus.DELIVERED
    assert n_comp.event_type == NotificationEventType.WORKFLOW_COMPLETED
    assert n_comp.priority == NotificationPriority.P2

    n_fail = manager.notify_workflow_failed(
        workflow_id="wf-beta",
        run_id="run-002",
        error_summary="Step compute_result failed after max retries.",
    )
    assert n_fail.status == NotificationStatus.DELIVERED
    assert n_fail.event_type == NotificationEventType.WORKFLOW_FAILED
    assert n_fail.priority == NotificationPriority.P1

    n_pause = manager.notify_workflow_paused(
        workflow_id="wf-gamma",
        run_id="run-003",
        reason="Step 'write_file' requires human approval.",
        project_id="proj-p",
    )
    assert n_pause.status == NotificationStatus.DELIVERED
    assert n_pause.event_type == NotificationEventType.WORKFLOW_PAUSED
    assert n_pause.priority == NotificationPriority.P1


# ==============================================================================
# TEST 14: DELIVERY ATTEMPT AUDIT TRAIL PERSISTED
# ==============================================================================


def test_14_delivery_attempt_audit_trail_persisted(
    store: NotificationStore,
    default_config: NotificationConfig,
) -> None:
    """Each delivery attempt is recorded in notification_attempts table."""
    mgr = NotificationManager(
        store=store,
        config=default_config,
        adapters=[AlwaysFailAdapter(), LocalLogAdapter()],
    )

    # AlwaysFailAdapter fails first, then LocalLogAdapter succeeds
    n = mgr.notify(
        event_type=NotificationEventType.GENERAL_INFO,
        title="Multi-adapter test",
        body="First adapter fails, second succeeds.",
    )
    assert n.status == NotificationStatus.DELIVERED

    attempts = store.list_attempts(n.notification_id)
    # Two adapters were tried in this one delivery call
    assert len(attempts) == 2
    # First attempt failed
    fail_attempt = next(a for a in attempts if a.adapter == "always_fail")
    assert not fail_attempt.succeeded
    # Second attempt succeeded
    success_attempt = next(a for a in attempts if a.adapter == "local_log")
    assert success_attempt.succeeded


# ==============================================================================
# TEST 15: RESOURCE CONSTRAINT AND RECOVERY EXHAUSTED NOTIFICATIONS
# ==============================================================================


def test_15_resource_constraint_and_recovery_exhausted(
    manager: NotificationManager,
) -> None:
    """RESOURCE_CONSTRAINT and RECOVERY_EXHAUSTED events produce correct priority."""
    n_res = manager.notify_resource_constraint(
        resource_name="GPU_MEMORY",
        detail="Insufficient VRAM to load model (need 8GB, have 4GB).",
        source_id="model-abc",
    )
    assert n_res.status == NotificationStatus.DELIVERED
    assert n_res.event_type == NotificationEventType.RESOURCE_CONSTRAINT
    assert n_res.priority == NotificationPriority.P2

    n_rec = manager.notify_recovery_exhausted(
        task_id="task-xyz",
        error_summary="All retry attempts failed, disk full.",
    )
    assert n_rec.status == NotificationStatus.DELIVERED
    assert n_rec.event_type == NotificationEventType.RECOVERY_EXHAUSTED
    assert n_rec.priority == NotificationPriority.P0  # Critical


# ==============================================================================
# TEST 16: OFFLINE OPERATION — NO NETWORK CALLS
# ==============================================================================


def test_16_offline_operation_no_network_calls(
    manager: NotificationManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notification subsystem operates 100% offline with zero network calls."""
    import socket as _socket

    def _block_network(self, address):  # type: ignore[no-untyped-def]
        raise RuntimeError(
            f"NETWORK BLOCKED: test_16 detected a network connection attempt to {address}"
        )

    monkeypatch.setattr(_socket.socket, "connect", _block_network)
    n = manager.notify(
        event_type=NotificationEventType.GENERAL_INFO,
        title="Offline check",
        body="This notification must not trigger any network call.",
    )
    assert n.status == NotificationStatus.DELIVERED
