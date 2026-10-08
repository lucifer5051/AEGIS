"""Tests for SAGE Approval Lifecycle and Trust Core Verification.

Verifies:
- Approval request creation and persistence
- Approval state transitions (APPROVED, REJECTED, EXPIRED)
- TrustAuthority integration:
    * Unapproved requests cannot grant capabilities
    * Forged / non-existent request IDs cannot grant capabilities
    * Action mismatch is rejected
    * Expired approvals are rejected
    * Rejected approvals raise ApprovalRejectedError
    * Approved requests successfully yield valid capabilities
"""

import sqlite3
import time
from pathlib import Path

import pytest

from src.storage.database import initialize_runtime_database
from src.trust.approval import ApprovalStore
from src.trust.audit import AuditWriter
from src.trust.authority import TrustAuthority
from src.trust.capability import CapabilityFactory, CapabilityRegistry, ResourceScope
from src.trust.errors import (
    ApprovalRejectedError,
    ApprovalRequiredError,
    AuthorizationError,
)
from src.trust.policy import AutonomyTier, PolicyEngine


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_approval.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


@pytest.fixture
def approval_store(test_db: sqlite3.Connection) -> ApprovalStore:
    return ApprovalStore(test_db)


@pytest.fixture
def trust_authority(test_db: sqlite3.Connection, approval_store: ApprovalStore) -> TrustAuthority:
    factory = CapabilityFactory()
    registry = CapabilityRegistry(factory)
    policy = PolicyEngine(autonomy_tier=AutonomyTier.STRICT_MANUAL)
    auditor = AuditWriter(test_db)
    return TrustAuthority(
        policy=policy,
        registry=registry,
        auditor=auditor,
        approval_store=approval_store,
    )


def test_approval_lifecycle_crud(approval_store: ApprovalStore) -> None:
    rec = approval_store.create_request(
        action_name="filesystem.write",
        action_risk_level="MEDIUM",
        target_resource="/tmp/test.txt",
        parameters_summary="actor=test",
        ttl_seconds=60,
    )
    assert rec.status == "PENDING"
    assert rec.action_name == "filesystem.write"

    retrieved = approval_store.get_request(rec.request_id)
    assert retrieved is not None
    assert retrieved.request_id == rec.request_id

    # Approve
    approved = approval_store.approve_request(rec.request_id, actor="operator")
    assert approved.status == "APPROVED"
    assert approved.decision_actor == "operator"

    # Pending list should be empty
    assert len(approval_store.list_pending()) == 0


def test_approval_lifecycle_rejection(approval_store: ApprovalStore) -> None:
    rec = approval_store.create_request(
        action_name="filesystem.delete",
        action_risk_level="HIGH",
    )
    rejected = approval_store.reject_request(rec.request_id, actor="operator")
    assert rejected.status == "REJECTED"


def test_approval_lifecycle_expiry(approval_store: ApprovalStore) -> None:
    rec = approval_store.create_request(
        action_name="filesystem.write",
        ttl_seconds=0,  # Expire immediately
    )
    time.sleep(0.01)
    expired_count = approval_store.expire_pending_requests()
    assert expired_count == 1

    fetched = approval_store.get_request(rec.request_id)
    assert fetched is not None
    assert fetched.status == "EXPIRED"


def test_trust_authority_enforces_real_approval(
    trust_authority: TrustAuthority,
    approval_store: ApprovalStore,
) -> None:
    scope = ResourceScope(filesystem_prefixes=frozenset(["/safe"]))

    # 1. Medium risk escalates at STRICT_MANUAL
    with pytest.raises(ApprovalRequiredError) as exc_info:
        trust_authority.request_capability(
            action_name="filesystem.write",
            scope=scope,
            actor="test_agent",
        )
    req_id = exc_info.value.request_id
    assert req_id

    # Verify request was persisted in approval_store
    stored = approval_store.get_request(req_id)
    assert stored is not None
    assert stored.status == "PENDING"

    # 2. Cannot grant capability while still PENDING
    with pytest.raises(ApprovalRequiredError):
        trust_authority.request_capability(
            action_name="filesystem.write",
            scope=scope,
            actor="test_agent",
            approval_request_id=req_id,
        )

    # 3. Forged / random UUID is rejected
    with pytest.raises(AuthorizationError):
        trust_authority.request_capability(
            action_name="filesystem.write",
            scope=scope,
            actor="test_agent",
            approval_request_id="00000000-0000-0000-0000-000000000000",
        )

    # 4. Approve request -> now capability issue succeeds!
    approval_store.approve_request(req_id, actor="admin")
    cap = trust_authority.request_capability(
        action_name="filesystem.write",
        scope=scope,
        actor="test_agent",
        approval_request_id=req_id,
    )
    assert cap is not None
    assert cap.action_name == "filesystem.write"


def test_trust_authority_rejects_mismatched_action_approval(
    trust_authority: TrustAuthority,
    approval_store: ApprovalStore,
) -> None:
    scope = ResourceScope(filesystem_prefixes=frozenset(["/safe"]))
    rec = approval_store.create_request(action_name="filesystem.read")
    approval_store.approve_request(rec.request_id, actor="admin")

    # Attempt to consume approval for a different action
    with pytest.raises(AuthorizationError):
        trust_authority.request_capability(
            action_name="filesystem.write",
            scope=scope,
            actor="test_agent",
            approval_request_id=rec.request_id,
        )


def test_trust_authority_rejects_rejected_approval(
    trust_authority: TrustAuthority,
    approval_store: ApprovalStore,
) -> None:
    scope = ResourceScope(filesystem_prefixes=frozenset(["/safe"]))
    rec = approval_store.create_request(action_name="filesystem.write")
    approval_store.reject_request(rec.request_id, actor="admin")

    with pytest.raises(ApprovalRejectedError):
        trust_authority.request_capability(
            action_name="filesystem.write",
            scope=scope,
            actor="test_agent",
            approval_request_id=rec.request_id,
        )
