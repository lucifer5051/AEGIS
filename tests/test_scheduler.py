"""SAGE Durable Scheduler -- Comprehensive Offline Integration Tests.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12

Tests:
1. One-time (ONE_SHOT) schedule execution and expiration.
2. Recurring interval and 5-part cron schedules.
3. Duplicate trigger prevention on restart / repeated polling.
4. Downtime missed-run handling: SKIP policy.
5. Downtime missed-run handling: RUN_ONCE policy.
6. Downtime missed-run handling: FAIL_ALERT policy.
7. Event-triggered condition dispatch.
8. Schedule pause, resume, and cancellation.
9. Zero-secret invariant in schedule parameters.
10. Deterministic 5-part cron expression parsing.
100% offline, zero network access.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.manager.verifier import StepVerifier
from src.scheduler import (
    MissedRunPolicy,
    ScheduleExecutionStatus,
    SchedulerStore,
    ScheduleStatus,
    ScheduleType,
    WorkflowScheduler,
    compute_next_cron_run,
)
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.errors import SecretLeakError
from src.trust.policy import AutonomyTier
from src.workflow import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStepDefinition,
    WorkflowStore,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    """Initialize fresh SQLite database with migrations 0001-0007 applied."""
    db_file = tmp_path / "test_scheduler.db"
    return initialize_runtime_database(db_file, migrations_dir=MIGRATIONS_DIR)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    sb = tmp_path / "sandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


@pytest.fixture
def scheduler_setup(db_conn: sqlite3.Connection, sandbox: Path):
    """Build complete pipeline with Scheduler, Engine, Store, and Supervisor."""
    workflow_store = WorkflowStore(db_conn)
    state_store = StateStore(db_conn)
    scheduler_store = SchedulerStore(db_conn)
    trust_auth = build_trust_authority(db_conn, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    tool_broker = ToolBroker(trust_auth)
    register_builtin_tools(tool_broker, allowed_roots=[sandbox])
    verifier = StepVerifier()

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        verifier=verifier,
    )

    engine = WorkflowEngine(
        workflow_store=workflow_store,
        state_store=state_store,
        supervisor=supervisor,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        verifier=verifier,
    )

    scheduler = WorkflowScheduler(
        scheduler_store=scheduler_store,
        workflow_engine=engine,
        missed_run_threshold_seconds=120.0,
    )

    # Register sample workflow for scheduled execution
    target_file = sandbox / "scheduled_output.txt"
    sample_wf = WorkflowDefinition(
        workflow_id="wf_scheduled_target",
        name="Scheduled Target Workflow",
        steps=[
            WorkflowStepDefinition(
                step_id="write_step",
                title="Write Scheduled Output",
                tool_name="file_write",
                arguments={"path": str(target_file), "content": "Scheduled Run Success"},
                is_idempotent=True,
            )
        ],
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )
    workflow_store.save_definition(sample_wf)

    return {
        "conn": db_conn,
        "scheduler": scheduler,
        "scheduler_store": scheduler_store,
        "workflow_store": workflow_store,
        "engine": engine,
        "sandbox": sandbox,
        "target_file": target_file,
    }


# ==============================================================================
# 1. ONE-SHOT SCHEDULE
# ==============================================================================


def test_01_one_shot_schedule_lifecycle(scheduler_setup):
    """ONE_SHOT schedule executes when due and transitions to EXPIRED."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]
    target_file: Path = scheduler_setup["target_file"]

    t0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    due_t = t0 + timedelta(minutes=10)

    sched = scheduler.create_schedule(
        schedule_id="sched_one_shot",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.ONE_SHOT,
        next_run_at=due_t.isoformat(),
    )
    assert sched.status == ScheduleStatus.ACTIVE

    # Poll before due: 0 executed
    recs = scheduler.poll_and_dispatch(current_time=t0)
    assert len(recs) == 0
    assert not target_file.exists()

    # Poll at due time: executes and expires
    recs = scheduler.poll_and_dispatch(current_time=due_t)
    assert len(recs) == 1
    assert recs[0].status == ScheduleExecutionStatus.COMPLETED
    assert target_file.exists()

    # Verify schedule marked EXPIRED
    store: SchedulerStore = scheduler_setup["scheduler_store"]
    updated = store.get_schedule("sched_one_shot")
    assert updated is not None
    assert updated.status == ScheduleStatus.EXPIRED

    # Subsequent poll does not execute again
    recs2 = scheduler.poll_and_dispatch(current_time=due_t + timedelta(minutes=5))
    assert len(recs2) == 0


# ==============================================================================
# 2. CRON RECURRING SCHEDULE
# ==============================================================================


def test_02_cron_recurring_schedule_evaluation(scheduler_setup):
    """CRON_RECURRING schedule advances next_run_at and executes repeatedly."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]
    store: SchedulerStore = scheduler_setup["scheduler_store"]

    # Every 15 minutes: "*/15 * * * *"
    scheduler.create_schedule(
        schedule_id="sched_cron",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.CRON_RECURRING,
        cron_expression="*/15 * * * *",
        next_run_at=datetime(2026, 10, 10, 10, 15, 0, tzinfo=timezone.utc).isoformat(),
    )

    t_first = datetime(2026, 10, 10, 10, 15, 0, tzinfo=timezone.utc)
    recs = scheduler.poll_and_dispatch(current_time=t_first)
    assert len(recs) == 1
    assert recs[0].status == ScheduleExecutionStatus.COMPLETED

    # Check next_run_at advanced to 10:30
    updated = store.get_schedule("sched_cron")
    assert updated is not None
    assert updated.status == ScheduleStatus.ACTIVE
    assert updated.next_run_at == "2026-10-10T10:30:00+00:00"

    # Second execution at 10:30
    t_second = datetime(2026, 10, 10, 10, 30, 0, tzinfo=timezone.utc)
    recs2 = scheduler.poll_and_dispatch(current_time=t_second)
    assert len(recs2) == 1

    updated2 = store.get_schedule("sched_cron")
    assert updated2 is not None
    assert updated2.next_run_at == "2026-10-10T10:45:00+00:00"


# ==============================================================================
# 3. DEDUPLICATION GUARANTEE
# ==============================================================================


def test_03_duplicate_schedule_trigger_prevention(scheduler_setup):
    """Repeated polling or application restart does not double-execute a slot."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]
    store: SchedulerStore = scheduler_setup["scheduler_store"]

    t_slot = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone.utc)

    scheduler.create_schedule(
        schedule_id="sched_dedup",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.ONE_SHOT,
        next_run_at=t_slot.isoformat(),
    )

    # First poll triggers execution
    recs1 = scheduler.poll_and_dispatch(current_time=t_slot)
    assert len(recs1) == 1

    # Simulate restart by manually setting next_run_at back to the same slot
    store.update_schedule_next_run(
        "sched_dedup",
        next_run_at=t_slot.isoformat(),
        status=ScheduleStatus.ACTIVE,
    )

    # Second poll detects slot already recorded; strictly prevents re-execution
    recs2 = scheduler.poll_and_dispatch(current_time=t_slot)
    assert len(recs2) == 0

    # Total recorded executions for this schedule is exactly 1
    execs = store.list_executions("sched_dedup")
    assert len(execs) == 1


# ==============================================================================
# 4. MISSED RUN HANDLING (DOWNTIME RECOVERY)
# ==============================================================================


def test_04_missed_run_policy_skip(scheduler_setup):
    """Missed run with policy SKIP logs a SKIPPED record and advances to future."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]
    store: SchedulerStore = scheduler_setup["scheduler_store"]

    # Scheduled 2 hours ago (downtime)
    past_due = datetime(2026, 10, 10, 8, 0, 0, tzinfo=timezone.utc)
    current_time = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)

    scheduler.create_schedule(
        schedule_id="sched_skip",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.CRON_RECURRING,
        interval_seconds=3600,
        next_run_at=past_due.isoformat(),
        missed_run_policy=MissedRunPolicy.SKIP,
    )

    recs = scheduler.poll_and_dispatch(current_time=current_time)
    assert len(recs) == 1
    assert recs[0].status == ScheduleExecutionStatus.SKIPPED
    assert "Skipped under policy SKIP" in (recs[0].result_summary or "")

    # next_run_at advanced past current_time
    updated = store.get_schedule("sched_skip")
    assert updated is not None
    assert datetime.fromisoformat(updated.next_run_at) > current_time


def test_05_missed_run_policy_run_once(scheduler_setup):
    """Missed run with policy RUN_ONCE executes once then advances to future."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]
    store: SchedulerStore = scheduler_setup["scheduler_store"]

    past_due = datetime(2026, 10, 10, 7, 0, 0, tzinfo=timezone.utc)
    current_time = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)

    scheduler.create_schedule(
        schedule_id="sched_run_once",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.CRON_RECURRING,
        interval_seconds=3600,
        next_run_at=past_due.isoformat(),
        missed_run_policy=MissedRunPolicy.RUN_ONCE,
    )

    recs = scheduler.poll_and_dispatch(current_time=current_time)
    assert len(recs) == 1
    assert recs[0].status == ScheduleExecutionStatus.COMPLETED

    updated = store.get_schedule("sched_run_once")
    assert updated is not None
    assert datetime.fromisoformat(updated.next_run_at) > current_time


def test_06_missed_run_policy_fail_alert(scheduler_setup):
    """Missed run with policy FAIL_ALERT records failure and pauses schedule."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]
    store: SchedulerStore = scheduler_setup["scheduler_store"]

    past_due = datetime(2026, 10, 10, 6, 0, 0, tzinfo=timezone.utc)
    current_time = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)

    scheduler.create_schedule(
        schedule_id="sched_fail_alert",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.CRON_RECURRING,
        interval_seconds=3600,
        next_run_at=past_due.isoformat(),
        missed_run_policy=MissedRunPolicy.FAIL_ALERT,
    )

    recs = scheduler.poll_and_dispatch(current_time=current_time)
    assert len(recs) == 1
    assert recs[0].status == ScheduleExecutionStatus.FAILED
    assert "Missed run alert" in (recs[0].result_summary or "")

    updated = store.get_schedule("sched_fail_alert")
    assert updated is not None
    assert updated.status == ScheduleStatus.PAUSED


# ==============================================================================
# 5. EVENT-TRIGGERED SCHEDULE
# ==============================================================================


def test_07_event_triggered_schedule(scheduler_setup):
    """EVENT_TRIGGERED schedule dispatches when matching event condition fires."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]

    scheduler.create_schedule(
        schedule_id="sched_event",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.EVENT_TRIGGERED,
        trigger_condition={"event": "file_uploaded"},
    )

    # Fire unmatched event: 0 executed
    recs_ignored = scheduler.trigger_event("unmatched_event")
    assert len(recs_ignored) == 0

    # Fire matching event: executes
    recs = scheduler.trigger_event(
        "file_uploaded",
        payload={"filename": "test.pdf"},
    )
    assert len(recs) == 1
    assert recs[0].status == ScheduleExecutionStatus.COMPLETED


# ==============================================================================
# 6. PAUSE, RESUME, CANCEL
# ==============================================================================


def test_08_schedule_pause_resume_cancel(scheduler_setup):
    """Pause stops dispatch; resume re-enables; cancel terminates permanently."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]
    store: SchedulerStore = scheduler_setup["scheduler_store"]

    t0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

    scheduler.create_schedule(
        schedule_id="sched_ctl",
        target_action="wf_scheduled_target",
        workflow_id="wf_scheduled_target",
        schedule_type=ScheduleType.ONE_SHOT,
        next_run_at=t0.isoformat(),
    )

    # 1. Pause
    scheduler.pause_schedule("sched_ctl")
    assert store.get_schedule("sched_ctl").status == ScheduleStatus.PAUSED

    # Poll: paused schedule is not executed
    recs = scheduler.poll_and_dispatch(current_time=t0)
    assert len(recs) == 0

    # 2. Resume
    scheduler.resume_schedule("sched_ctl")
    assert store.get_schedule("sched_ctl").status == ScheduleStatus.ACTIVE

    # Poll: now executes
    recs2 = scheduler.poll_and_dispatch(current_time=t0)
    assert len(recs2) == 1

    # 3. Cancel
    scheduler.cancel_schedule("sched_ctl")
    assert store.get_schedule("sched_ctl").status == ScheduleStatus.CANCELLED


# ==============================================================================
# 7. SECRET PROTECTION & CRON PARSING
# ==============================================================================


def test_09_secret_rejection_in_schedule(scheduler_setup):
    """Schedule parameters containing raw secrets raise SecretLeakError."""
    scheduler: WorkflowScheduler = scheduler_setup["scheduler"]

    with pytest.raises(SecretLeakError):
        scheduler.create_schedule(
            schedule_id="sched_leaky",
            target_action="wf_scheduled_target",
            schedule_type=ScheduleType.ONE_SHOT,
            next_run_at="2026-10-10T12:00:00Z",
            parameters={"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"},
        )


def test_10_deterministic_cron_evaluator():
    """Validates 5-part cron syntax parsing and next run calculation."""
    t0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

    # Run at minute 15 of every hour
    next_t = compute_next_cron_run("15 * * * *", t0)
    assert next_t == datetime(2026, 10, 10, 12, 15, 0, tzinfo=timezone.utc)

    # Every 5 minutes
    next_step = compute_next_cron_run("*/5 * * * *", t0)
    assert next_step == datetime(2026, 10, 10, 12, 5, 0, tzinfo=timezone.utc)

    # Every weekday at 09:00 (1-5 = Mon-Fri; 2026-10-10 is a Saturday!)
    # Saturday is dow 6, so next weekday is Monday 2026-10-12
    next_mon = compute_next_cron_run("0 9 * * 1-5", t0)
    assert next_mon.weekday() == 0  # Monday
    assert next_mon.hour == 9
    assert next_mon.minute == 0
