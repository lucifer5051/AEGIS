"""SAGE Trust Core -- Deterministic Trust Authority.

This is the single public interface for all authorization decisions in SAGE.

Core invariants (from spec section 17 and principle "Intelligence proposes;
deterministic authority decides"):

1. No LLM output may directly invoke a system call without traversal through
   this module (the Security Invariant).
2. All authorization decisions are deterministic; no model inference, no
   probability reasoning.
3. All decisions are audited; if audit fails, the action is denied.
4. Fail-closed: any unexpected error results in DENY.
5. No secrets in any log, exception message, or audit record.
6. An LLM/provider may NEVER:
   - Grant itself a capability.
   - Modify policy.
   - Bypass capability checks.
   - Authorize its own sensitive action.
   - Directly access protected resources.

Usage pattern:
    authority = TrustAuthority(policy_engine, capability_registry, audit_writer)
    cap = authority.request_capability("filesystem.write", scope, actor="fast_decision")
    authority.consume_capability(cap, target_path="/home/user/project/foo.py")
    # ... perform the action ...
"""

import logging
from typing import Optional
from uuid import uuid4

from src.trust.audit import AuditDecision, AuditEventType, AuditWriter
from src.trust.capability import (
    Capability,
    CapabilityRegistry,
    ResourceScope,
)
from src.trust.errors import (
    ApprovalRequiredError,
    CapabilityError,
    PolicyDeniedError,
    TrustError,
)
from src.trust.policy import PolicyDecision, PolicyEngine

logger = logging.getLogger(__name__)


class TrustAuthority:
    """Central deterministic authorization authority for SAGE.

    All capability issuance and authorization runs through this class.
    It composes:
      - PolicyEngine   -> classifies actions and decides allow/escalate/deny
      - CapabilityRegistry -> issues, tracks, and validates tokens
      - AuditWriter    -> persists every decision to the audit log

    The authority is NOT a singleton; callers instantiate it per-session or
    per-component and inject the shared DB connection.

    Thread-safety note: PolicyEngine and CapabilityRegistry use stdlib
    primitives that are GIL-protected for simple operations, but compound
    operations (e.g. check-then-set) are NOT thread-safe.  For production use,
    callers should serialize calls to request_capability() and
    consume_capability() with an external lock.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        registry: CapabilityRegistry,
        auditor: AuditWriter,
    ) -> None:
        self._policy = policy
        self._registry = registry
        self._auditor = auditor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_capability(
        self,
        action_name: str,
        scope: ResourceScope,
        actor: str,
        project_id: Optional[str] = None,
        ttl_seconds: float = 60.0,
        max_uses: int = 1,
        approval_request_id: Optional[str] = None,
    ) -> Capability:
        """Evaluate policy and, if permitted, issue a signed capability token.

        Args:
            action_name:          Canonical action label (e.g. "filesystem.write").
            scope:                Resource boundary for this capability.
            actor:                Identity of the requesting component.
            project_id:           Optional project context.
            ttl_seconds:          Token lifetime in seconds (default 60 s).
            max_uses:             Max consumptions (default 1 = single-use).
            approval_request_id:  If provided, treat this request as an
                                  already-approved escalation (must match DB
                                  state; validated by caller before passing).

        Returns:
            A signed Capability token.

        Raises:
            PolicyDeniedError:     Hard DENY -- no approval path.
            ApprovalRequiredError: ESCALATE -- caller must obtain approval.
            AuditError:            Audit write failed (fail-closed).
            TrustError:            Any other internal failure (fail-closed).
        """
        try:
            policy_result = self._policy.evaluate(action_name, project_id=project_id)
        except Exception as exc:
            # Policy evaluation must never silently fail
            self._safe_audit(
                event_type=AuditEventType.AUTHZ_DECISION,
                actor=actor,
                target_entity_type="action",
                target_entity_id=action_name,
                decision=AuditDecision.DENY,
                policy_rule_id="RULE:POLICY_EVAL_ERROR",
                project_id=project_id,
                telemetry={"error": type(exc).__name__},
            )
            raise TrustError(
                f"Policy evaluation error for action '{action_name}': {type(exc).__name__}"
            ) from exc

        # --- Hard DENY ---
        if policy_result.decision == PolicyDecision.DENY:
            self._safe_audit(
                event_type=AuditEventType.AUTHZ_DECISION,
                actor=actor,
                target_entity_type="action",
                target_entity_id=action_name,
                decision=AuditDecision.DENY,
                policy_rule_id=policy_result.rule_id,
                project_id=project_id,
                telemetry={"rationale": policy_result.rationale, "risk": policy_result.risk.value},
            )
            raise PolicyDeniedError(
                f"Action '{action_name}' is permanently denied by policy. "
                f"Rule: {policy_result.rule_id}"
            )

        # --- ESCALATE (approval required) ---
        if policy_result.decision == PolicyDecision.ESCALATE and approval_request_id is None:
            req_id = str(uuid4())
            self._safe_audit(
                event_type=AuditEventType.AUTHZ_DECISION,
                actor=actor,
                target_entity_type="action",
                target_entity_id=action_name,
                decision=AuditDecision.ESCALATE,
                policy_rule_id=policy_result.rule_id,
                project_id=project_id,
                telemetry={
                    "rationale": policy_result.rationale,
                    "risk": policy_result.risk.value,
                    "approval_request_id": req_id,
                },
            )
            raise ApprovalRequiredError(
                f"Action '{action_name}' requires user approval "
                f"(risk={policy_result.risk.value}, tier={policy_result.autonomy_tier.name}). "
                f"Submit approval request {req_id!r}.",
                request_id=req_id,
            )

        # --- ALLOW (or approved escalation) ---
        capability_id = str(uuid4())
        cap = self._registry.issue(
            capability_id=capability_id,
            action_name=action_name,
            risk=policy_result.risk,
            scope=scope,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
        )

        telemetry: dict = {
            "capability_id": capability_id,
            "action_name": action_name,
            "risk": policy_result.risk.value,
            "ttl_seconds": ttl_seconds,
            "max_uses": max_uses,
        }
        if approval_request_id:
            telemetry["approval_request_id"] = approval_request_id

        self._safe_audit(
            event_type=AuditEventType.CAPABILITY_ISSUED,
            actor=actor,
            target_entity_type="capability",
            target_entity_id=capability_id,
            decision=AuditDecision.ALLOW,
            policy_rule_id=policy_result.rule_id,
            project_id=project_id,
            telemetry=telemetry,
        )

        return cap

    def consume_capability(
        self,
        cap: Capability,
        actor: str,
        target_path: Optional[str] = None,
        target_host: Optional[str] = None,
        target_command: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Validate and consume one use of a capability.

        Must be called immediately before executing the gated action.

        Raises:
            CapabilityError subclass:  Any capability lifecycle failure.
            AuditError:               Audit write failed (fail-closed).
        """
        try:
            self._registry.consume(
                cap,
                target_path=target_path,
                target_host=target_host,
                target_command=target_command,
            )
        except CapabilityError as exc:
            self._safe_audit(
                event_type=AuditEventType.AUTHZ_DECISION,
                actor=actor,
                target_entity_type="capability",
                target_entity_id=cap.capability_id,
                decision=AuditDecision.DENY,
                policy_rule_id="RULE:CAPABILITY_LIFECYCLE",
                project_id=project_id,
                telemetry={
                    "error": type(exc).__name__,
                    "action_name": cap.action_name,
                    "risk": cap.risk.value,
                },
            )
            raise

        self._safe_audit(
            event_type=AuditEventType.AUTHZ_DECISION,
            actor=actor,
            target_entity_type="capability",
            target_entity_id=cap.capability_id,
            decision=AuditDecision.ALLOW,
            policy_rule_id="RULE:CAPABILITY_CONSUMED",
            project_id=project_id,
            telemetry={
                "action_name": cap.action_name,
                "risk": cap.risk.value,
            },
        )

    def revoke_capability(
        self,
        capability_id: str,
        actor: str,
        project_id: Optional[str] = None,
    ) -> None:
        """Immediately revoke a capability by ID."""
        self._registry.revoke(capability_id)
        self._safe_audit(
            event_type=AuditEventType.CAPABILITY_REVOKED,
            actor=actor,
            target_entity_type="capability",
            target_entity_id=capability_id,
            decision=AuditDecision.DENY,
            policy_rule_id="RULE:CAPABILITY_REVOKED",
            project_id=project_id,
            telemetry={"capability_id": capability_id},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_audit(
        self,
        event_type: AuditEventType,
        actor: str,
        target_entity_type: str,
        target_entity_id: str,
        decision: AuditDecision,
        policy_rule_id: str,
        telemetry: Optional[dict] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Write an audit record; raise AuditError on any failure (fail-closed)."""
        self._auditor.write(
            event_type=event_type,
            actor=actor,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            decision=decision,
            policy_rule_id=policy_rule_id,
            telemetry=telemetry,
            project_id=project_id,
        )
