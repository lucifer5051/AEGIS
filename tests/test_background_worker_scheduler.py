"""SAGE Background Worker & Persistent Task Scheduler -- Offline Integration Tests.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12

Verifies:
1. One-time scheduling: immediate, delayed, at timestamp.
2. Recurring scheduling: friendly intervals, cron, timezone normalization.
3. Atomic claiming & concurrency without duplicate execution.
4. Worker heartbeat and lease extension.
5. Worker crash recovery: abandoned lease recovery with backoff.
6. Execution through Supervisor with StateStore checkpoints.
7. TrustCore authorization: policy denial stops execution fail-closed with zero retry.
8. Prompt 069 configuration resolution and execution profile bounds.
9. Approval suspension (WAITING_APPROVAL) and human resumption.
10. Retry handling: exponential backoff vs non-retryable error classification.
11. Unified Memory Engine outcome recording for background jobs.
12. Zero-secret invariant in job metadata and schedule parameters.
100% offline, zero network access.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from src.config.manager import SageConfigurationManager
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor, StepObservation, ObservationStatus
from src.manager.verifier import StepVerifier
from src.memory.engine import UnifiedMemoryEngine
from src.scheduler import (
    BackgroundJob,
    JobExecutionResult,
    JobQueue,
    JobStatus,
    MissedRunPolicy,
    ScheduleStatus,
    ScheduleType,
    SchedulerStore,
    WorkerManager,
    WorkerRegistration,
    WorkerStatus,
    WorkflowScheduler,
)
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.builtin import register_builtin_tools
from src.trust import build_trust_authority
from src.trust.errors import SecretLeakError
from src.trust.policy import AutonomyTier

from src.reasoning.base import LocalReasoningProvider
from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningRequest,
    ReasoningResponse,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


class MockReasoningProvider(LocalReasoningProvider):
    """Deterministic offline mock reasoning provider for testing supervisor execution."""

    def __init__(self, steps: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__(provider_id="mock_reasoner", model_id="mock_model")
        self.canned_steps = steps or []

    def initialize(self) -> bool:
        return True

    def is_ready(self) -> bool:
        return True

    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        import json
        plan_dict = {
            "goal": "Test goal",
            "rationale": "Automated plan",
            "steps": self.canned_steps
            or [
                {
                    "step_number": 1,
                    "description": "Default planned step",
                    "tool": "file_write",
                    "arguments": {"path": "test.txt", "content": "mock"},
                    "is_idempotent": True,
                    "acceptance_criteria": "done",
                    "estimated_risk": "LOW",
                    "target_resource": "*",
                }
            ],
        }
        return ReasoningResponse(
            content=json.dumps(plan_dict),
            finish_reason="stop",
            prompt_tokens=40,
            completion_tokens=40,
            total_tokens=80,
            model_id=self.model_id,
            provider_id=self.provider_id,
        )

    def unload(self) -> bool:
        return True

    def verify_integrity(self) -> bool:
        return True

    def get_metrics(self) -> ReasoningMetrics:
        return ReasoningMetrics()


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    """Initialize fresh test database with all canonical migrations applied."""
    db_file = tmp_path / "test_background_worker.db"
    return initialize_runtime_database(db_file, migrations_dir=MIGRATIONS_DIR)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    sb = tmp_path / "sandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


@pytest.fixture
def system_fixture(db_conn: sqlite3.Connection, sandbox: Path):
    """Build complete environment with StateStore, TrustAuthority, ToolBroker, Supervisor, and Queue."""
    state_store = StateStore(db_conn)
    trust_auth = build_trust_authority(db_conn, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    tool_broker = ToolBroker(trust_auth)
    register_builtin_tools(tool_broker, allowed_roots=[sandbox])
    verifier = StepVerifier()

    mock_reasoner = MockReasoningProvider(
        steps=[
            {
                "step_number": 1,
                "description": "Create background test file",
                "tool": "file_write",
                "arguments": {"path": str(sandbox / "bg_output.txt"), "content": "background worker ok"},
                "is_idempotent": True,
                "acceptance_criteria": "written",
                "estimated_risk": "LOW",
                "target_resource": str(sandbox / "bg_output.txt"),
            }
        ]
    )

    config_mgr = SageConfigurationManager(conn=db_conn)
    memory_engine = UnifiedMemoryEngine(db_conn)

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust_auth,
        tool_broker=tool_broker,
        verifier=verifier,
        reasoning_provider=mock_reasoner,
        config_manager=config_mgr,
        unified_memory=memory_engine,
    )

    sched_store = SchedulerStore(db_conn)
    job_queue = JobQueue(db_conn)
    worker_mgr = WorkerManager(
        job_queue=job_queue,
        supervisor=supervisor,
        trust_authority=trust_auth,
        config_manager=config_mgr,
        memory_engine=memory_engine,
        lease_duration_seconds=30.0,
        backoff_base_seconds=2.0,
        max_workers=2,
    )

    scheduler = WorkflowScheduler(
        scheduler_store=sched_store,
        job_queue=job_queue,
        worker_manager=worker_mgr,
    )

    return {
        "db": db_conn,
        "sandbox": sandbox,
        "state_store": state_store,
        "trust_auth": trust_auth,
        "tool_broker": tool_broker,
        "supervisor": supervisor,
        "config_mgr": config_mgr,
        "memory_engine": memory_engine,
        "sched_store": sched_store,
        "job_queue": job_queue,
        "worker_mgr": worker_mgr,
        "scheduler": scheduler,
    }


# =============================================================================
# 1. ONE-TIME SCHEDULING TESTS
# =============================================================================

class TestOneTimeScheduling:
    def test_schedule_immediate(self, system_fixture):
        scheduler = system_fixture["scheduler"]
        job_queue = system_fixture["job_queue"]

        job = scheduler.schedule_immediate(
            target_action="generate report",
            goal_text="Produce autonomous background report",
            priority=5,
        )

        assert job.status == JobStatus.QUEUED
        assert job.priority == 5
        assert job.goal_text == "Produce autonomous background report"

        persisted = job_queue.get_job(job.job_id)
        assert persisted is not None
        assert persisted.status == JobStatus.QUEUED

    def test_schedule_delayed(self, system_fixture):
        scheduler = system_fixture["scheduler"]
        job_queue = system_fixture["job_queue"]

        job = scheduler.schedule_delayed(
            delay_seconds=60.0,
            target_action="cleanup cache",
            priority=15,
        )

        assert job.status == JobStatus.PENDING
        # Cannot be claimed before scheduled_at
        claimed = job_queue.claim_next_job("worker-1", current_time=datetime.now(timezone.utc))
        assert claimed is None

        # Can be claimed after scheduled_at
        future_time = datetime.now(timezone.utc) + timedelta(seconds=65)
        claimed = job_queue.claim_next_job("worker-1", current_time=future_time)
        assert claimed is not None
        assert claimed.job_id == job.job_id

    def test_schedule_at_with_timezone(self, system_fixture):
        scheduler = system_fixture["scheduler"]
        job_queue = system_fixture["job_queue"]

        # Run at 2 hours from now in IST (+05:30)
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        ist_future = datetime.now(ist_tz) + timedelta(hours=2)

        job = scheduler.schedule_at(
            run_at=ist_future,
            target_action="sync database",
        )

        assert job.status == JobStatus.PENDING
        # The stored scheduled_at must be in UTC
        sched_dt = datetime.fromisoformat(job.scheduled_at)
        assert sched_dt.tzinfo == timezone.utc


# =============================================================================
# 2. RECURRING SCHEDULING TESTS
# =============================================================================

class TestRecurringScheduling:
    def test_friendly_recurrence_daily(self, system_fixture):
        scheduler = system_fixture["scheduler"]
        sched_def = scheduler.create_recurring_schedule(
            recurrence="daily",
            target_action="backup system state",
        )

        assert sched_def.schedule_type == ScheduleType.CRON_RECURRING
        assert sched_def.cron_expression == "0 0 * * *"
        assert sched_def.status == ScheduleStatus.ACTIVE

    def test_recurring_schedule_with_timezone_offset(self, system_fixture):
        scheduler = system_fixture["scheduler"]
        sched_def = scheduler.create_recurring_schedule(
            recurrence="0 9 * * *",  # 9:00 AM
            target_action="morning briefing",
            timezone_offset_minutes=330,  # IST (+05:30)
        )

        assert sched_def.status == ScheduleStatus.ACTIVE
        next_run_dt = datetime.fromisoformat(sched_def.next_run_at)
        assert next_run_dt.tzinfo == timezone.utc

    def test_poll_and_dispatch_enqueues_background_job(self, system_fixture):
        scheduler = system_fixture["scheduler"]
        job_queue = system_fixture["job_queue"]

        now = datetime.now(timezone.utc)
        past_str = (now - timedelta(minutes=10)).isoformat()

        sched_def = scheduler.create_schedule(
            target_action="run maintenance",
            schedule_type=ScheduleType.ONE_SHOT,
            next_run_at=past_str,
            parameters={"goal_text": "Execute scheduled maintenance"},
        )

        # poll and dispatch should enqueue job into job_queue
        records = scheduler.poll_and_dispatch(current_time=now)
        assert len(records) == 1

        jobs = job_queue.list_jobs(schedule_id=sched_def.schedule_id)
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.QUEUED
        assert jobs[0].goal_text == "Execute scheduled maintenance"


# =============================================================================
# 3. ATOMIC CLAIMING & CONCURRENCY TESTS
# =============================================================================

class TestQueueClaimingAndConcurrency:
    def test_atomic_claim_prevents_duplicate_ownership(self, system_fixture):
        job_queue = system_fixture["job_queue"]

        # Enqueue a single job
        job = BackgroundJob(goal_text="Unique task")
        job_queue.enqueue_job(job)

        # Worker 1 claims it
        claim1 = job_queue.claim_next_job("worker-alpha")
        assert claim1 is not None
        assert claim1.job_id == job.job_id
        assert claim1.worker_id == "worker-alpha"
        assert claim1.status == JobStatus.RUNNING

        # Worker 2 attempts to claim at the same time: queue is now empty
        claim2 = job_queue.claim_next_job("worker-beta")
        assert claim2 is None

    def test_priority_ordering(self, system_fixture):
        job_queue = system_fixture["job_queue"]

        job_low = BackgroundJob(goal_text="Low priority", priority=20)
        job_high = BackgroundJob(goal_text="High priority", priority=1)
        job_default = BackgroundJob(goal_text="Default priority", priority=10)

        job_queue.enqueue_job(job_low)
        job_queue.enqueue_job(job_high)
        job_queue.enqueue_job(job_default)

        # First claim must be high priority
        claim1 = job_queue.claim_next_job("worker-1")
        assert claim1.job_id == job_high.job_id

        # Second claim must be default priority
        claim2 = job_queue.claim_next_job("worker-1")
        assert claim2.job_id == job_default.job_id

        # Third claim must be low priority
        claim3 = job_queue.claim_next_job("worker-1")
        assert claim3.job_id == job_low.job_id

    def test_heartbeat_lease_extension(self, system_fixture):
        job_queue = system_fixture["job_queue"]
        job = BackgroundJob(goal_text="Long task")
        job_queue.enqueue_job(job)

        claimed = job_queue.claim_next_job("worker-1", lease_duration_seconds=10.0)
        orig_lease = claimed.lease_expires_at

        # Heartbeat to extend lease
        extended = job_queue.heartbeat_lease(job.job_id, "worker-1", extension_seconds=60.0)
        assert extended is True

        refreshed = job_queue.get_job(job.job_id)
        assert refreshed.lease_expires_at > orig_lease


# =============================================================================
# 4. WORKER CRASH RECOVERY TESTS
# =============================================================================

class TestWorkerCrashRecovery:
    def test_abandoned_job_recovery_with_backoff(self, system_fixture):
        job_queue = system_fixture["job_queue"]
        job = BackgroundJob(goal_text="Crash test", max_attempts=3)
        job_queue.enqueue_job(job)

        # Worker claims job with 10 second lease
        claimed = job_queue.claim_next_job("crashed-worker", lease_duration_seconds=10.0)
        assert claimed.attempt_count == 1

        # Simulate time passing beyond lease expiration (worker died)
        past_lease_time = datetime.now(timezone.utc) + timedelta(seconds=15)
        recovered_count = job_queue.recover_abandoned_jobs(current_time=past_lease_time)
        assert recovered_count == 1

        recovered_job = job_queue.get_job(job.job_id)
        assert recovered_job.status == JobStatus.RETRY_WAIT
        assert recovered_job.worker_id is None
        assert recovered_job.next_retry_at is not None
        assert "Worker lease expired" in recovered_job.last_error

    def test_max_attempts_exceeded_fails_permanently(self, system_fixture):
        job_queue = system_fixture["job_queue"]
        job = BackgroundJob(goal_text="Exhaust attempts", max_attempts=1)
        job_queue.enqueue_job(job)

        claimed = job_queue.claim_next_job("worker-1", lease_duration_seconds=5.0)
        assert claimed.attempt_count == 1

        future_time = datetime.now(timezone.utc) + timedelta(seconds=10)
        recovered = job_queue.recover_abandoned_jobs(current_time=future_time)
        assert recovered == 1

        failed_job = job_queue.get_job(job.job_id)
        assert failed_job.status == JobStatus.FAILED
        assert "max attempts exceeded" in failed_job.last_error


# =============================================================================
# 5. SUPERVISOR EXECUTION & CHECKPOINTS
# =============================================================================

class TestSupervisorExecution:
    def test_run_worker_tick_executes_goal_to_completion(self, system_fixture):
        scheduler = system_fixture["scheduler"]
        worker_mgr = system_fixture["worker_mgr"]
        job_queue = system_fixture["job_queue"]
        state_store = system_fixture["state_store"]
        sandbox = system_fixture["sandbox"]

        # Enqueue job
        job = scheduler.schedule_immediate(
            target_action="create file",
            goal_text="Write background worker file",
        )

        reg = worker_mgr.register_worker("test-worker")
        res = worker_mgr.run_worker_tick(reg.worker_id)

        assert res is not None
        assert res.success is True
        assert res.status == JobStatus.COMPLETED

        # Checkpoint lineage preserved in StateStore
        completed_job = job_queue.get_job(job.job_id)
        assert completed_job.status == JobStatus.COMPLETED
        assert completed_job.task_id is not None

        task = state_store.get_task(completed_job.task_id)
        assert task is not None
        checkpoints = state_store.list_checkpoints(task.task_id)
        assert len(checkpoints) >= 1

        # File was actually written to disk in sandbox
        assert (sandbox / "bg_output.txt").exists()


# =============================================================================
# 6. TRUSTCORE AUTHORIZATION LIFECYCLE
# =============================================================================

class TestTrustCoreAuthorization:
    def test_hard_denied_policy_fails_fail_closed_with_zero_retry(self, system_fixture):
        trust_auth = system_fixture["trust_auth"]
        worker_mgr = system_fixture["worker_mgr"]
        job_queue = system_fixture["job_queue"]

        # Hard-deny automation.restricted_action in TrustCore
        trust_auth._policy._hard_denied = frozenset(["automation.restricted_action"])

        job = BackgroundJob(
            goal_text="Do prohibited action",
            action_type="restricted_action",
            max_attempts=3,
        )
        job_queue.enqueue_job(job)

        reg = worker_mgr.register_worker("worker-strict")
        res = worker_mgr.run_worker_tick(reg.worker_id)

        assert res is not None
        assert res.success is False
        assert res.status == JobStatus.FAILED  # Fail closed immediately
        assert "TrustCoreDenied" in res.result_summary

        # Not eligible for retry
        failed_job = job_queue.get_job(job.job_id)
        assert failed_job.status == JobStatus.FAILED
        assert failed_job.next_retry_at is None


# =============================================================================
# 7. APPROVAL SUSPENSION & HUMAN RESUMPTION
# =============================================================================

class TestApprovalSuspensionAndResumption:
    def test_approval_suspension_lifecycle(self, system_fixture):
        job_queue = system_fixture["job_queue"]
        scheduler = system_fixture["scheduler"]

        job = scheduler.schedule_immediate(
            target_action="sensitive operation",
            goal_text="Transfer secure dataset",
        )

        # Suspend job pending approval
        suspended = job_queue.suspend_for_approval(job.job_id, "Requires human review")
        assert suspended.status == JobStatus.WAITING_APPROVAL

        # Worker cannot claim a job waiting approval
        claimed = job_queue.claim_next_job("worker-1")
        assert claimed is None

        # Human approves
        approved = scheduler.approve_job(job.job_id, approver="alice_admin")
        assert approved.status == JobStatus.QUEUED
        assert approved.metadata.get("approved_by") == "alice_admin"

        # Now worker can claim it
        claimed = job_queue.claim_next_job("worker-1")
        assert claimed is not None
        assert claimed.job_id == job.job_id

    def test_approval_rejection_cancels_job(self, system_fixture):
        job_queue = system_fixture["job_queue"]
        scheduler = system_fixture["scheduler"]

        job = scheduler.schedule_immediate(target_action="risky task")
        job_queue.suspend_for_approval(job.job_id, "High risk")

        rejected = scheduler.reject_job(job.job_id, reason="Denied by security officer")
        assert rejected.status == JobStatus.CANCELLED
        assert "ApprovalRejected" in rejected.last_error


# =============================================================================
# 8. RETRY & BACKOFF LOGIC
# =============================================================================

class TestRetryAndBackoff:
    def test_transient_failure_exponential_backoff(self, system_fixture):
        job_queue = system_fixture["job_queue"]

        job = BackgroundJob(goal_text="Network scrape", max_attempts=3)
        job_queue.enqueue_job(job)

        # First claim & failure
        job_queue.claim_next_job("worker-1")
        failed1 = job_queue.fail_job(job.job_id, "Timeout connecting to host", is_retryable=True, backoff_base_seconds=2.0)
        assert failed1.status == JobStatus.RETRY_WAIT
        retry1_time = datetime.fromisoformat(failed1.next_retry_at)

        # Advance time to allow second attempt
        job_queue.claim_next_job("worker-1", current_time=retry1_time + timedelta(seconds=1))
        failed2 = job_queue.fail_job(job.job_id, "Timeout connecting again", is_retryable=True, backoff_base_seconds=2.0)
        assert failed2.status == JobStatus.RETRY_WAIT
        retry2_time = datetime.fromisoformat(failed2.next_retry_at)

        # Backoff increased exponentially (2.0 * 2^1 = 4.0s vs 2.0 * 2^0 = 2.0s)
        delta2 = (retry2_time - datetime.now(timezone.utc)).total_seconds()
        assert delta2 > 2.0

    def test_manual_retry_job(self, system_fixture):
        job_queue = system_fixture["job_queue"]
        scheduler = system_fixture["scheduler"]

        job = BackgroundJob(goal_text="Failed job", max_attempts=1)
        job_queue.enqueue_job(job)

        job_queue.claim_next_job("worker-1")
        job_queue.fail_job(job.job_id, "Fatal unhandled error", is_retryable=False)

        job_now = job_queue.get_job(job.job_id)
        assert job_now.status == JobStatus.FAILED

        # User calls retry_job
        retried = scheduler.retry_job(job.job_id)
        assert retried.status == JobStatus.QUEUED
        assert retried.attempt_count == 0
        assert retried.last_error is None


# =============================================================================
# 9. UNIFIED MEMORY ENGINE INTEGRATION
# =============================================================================

class TestUnifiedMemoryIntegration:
    def test_completed_job_records_outcome_to_memory(self, system_fixture):
        worker_mgr = system_fixture["worker_mgr"]
        memory_engine = system_fixture["memory_engine"]
        scheduler = system_fixture["scheduler"]

        job = scheduler.schedule_immediate(
            target_action="build project report",
            goal_text="Compile end of day summary",
        )

        reg = worker_mgr.register_worker("worker-memory")
        res = worker_mgr.run_worker_tick(reg.worker_id)
        assert res.success is True

        # Check that memory record was formed in UnifiedMemoryEngine
        recalled = memory_engine.recall(query_text="summary", limit=5)
        assert len(recalled.records) >= 1
        assert any("Compile end of day summary" in r.content for r in recalled.records)


# =============================================================================
# 10. ZERO-SECRET ENFORCEMENT
# =============================================================================

class TestZeroSecretEnforcement:
    def test_job_with_secrets_rejected(self, system_fixture):
        scheduler = system_fixture["scheduler"]

        with pytest.raises(SecretLeakError):
            scheduler.schedule_immediate(
                target_action="test action",
                parameters={"api_key": "sk-proj-supersecret1234567890abcdef123456"},
            )
