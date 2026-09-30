"""AEGIS Trust Core -- Public API Surface.

"Intelligence proposes; deterministic authority decides."

Import the high-level factory function to get a ready-to-use TrustAuthority:

    from src.trust import build_trust_authority
    authority = build_trust_authority(conn, autonomy_tier=AutonomyTier.STRICT_MANUAL)

All other submodules (capability, policy, audit) can be imported directly if
a caller needs access to lower-level types (e.g. ResourceScope, ActionRisk).
"""

import sqlite3

from src.trust.audit import AuditDecision, AuditEventType, AuditWriter, redact_secret_hash
from src.trust.authority import TrustAuthority
from src.trust.capability import (
    ActionRisk,
    Capability,
    CapabilityFactory,
    CapabilityRegistry,
    ResourceScope,
)
from src.trust.errors import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalRequiredError,
    AuditError,
    AuthorizationError,
    CapabilityError,
    CapabilityExhaustedError,
    CapabilityExpiredError,
    CapabilityForgedError,
    CapabilityRevokedError,
    CapabilityScopeError,
    PolicyConfigurationError,
    PolicyDeniedError,
    SecretLeakError,
    TrustError,
)
from src.trust.policy import AutonomyTier, PolicyDecision, PolicyEngine, PolicyResult


def build_trust_authority(
    conn: sqlite3.Connection,
    autonomy_tier: AutonomyTier = AutonomyTier.STRICT_MANUAL,
) -> TrustAuthority:
    """Construct a fully-wired TrustAuthority for the given DB connection.

    Creates a new CapabilityFactory (with fresh process-local signing key),
    a CapabilityRegistry backed by it, a PolicyEngine at the specified
    autonomy tier, and an AuditWriter bound to *conn*.

    Args:
        conn:          An open SQLite connection with the trust-core schema
                       (migration 0005_trust_core_schema.sql applied).
        autonomy_tier: Initial operational autonomy tier (default: STRICT_MANUAL).

    Returns:
        A ready-to-use TrustAuthority instance.
    """
    factory = CapabilityFactory()
    registry = CapabilityRegistry(factory)
    policy = PolicyEngine(autonomy_tier=autonomy_tier)
    auditor = AuditWriter(conn)
    return TrustAuthority(policy=policy, registry=registry, auditor=auditor)


__all__ = [
    # Authority
    "TrustAuthority",
    "build_trust_authority",
    # Capability
    "ActionRisk",
    "Capability",
    "CapabilityFactory",
    "CapabilityRegistry",
    "ResourceScope",
    # Policy
    "AutonomyTier",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyResult",
    # Audit
    "AuditDecision",
    "AuditEventType",
    "AuditWriter",
    "redact_secret_hash",
    # Errors
    "TrustError",
    "CapabilityError",
    "CapabilityForgedError",
    "CapabilityExpiredError",
    "CapabilityRevokedError",
    "CapabilityExhaustedError",
    "CapabilityScopeError",
    "AuthorizationError",
    "PolicyDeniedError",
    "ApprovalRequiredError",
    "ApprovalExpiredError",
    "ApprovalRejectedError",
    "AuditError",
    "SecretLeakError",
    "PolicyConfigurationError",
]
