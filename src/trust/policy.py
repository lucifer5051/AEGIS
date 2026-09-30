"""AEGIS Trust Core -- Deterministic Policy Engine.

Responsibilities:
- Classify any action by its canonical ActionRisk (LOW/MEDIUM/HIGH/CRITICAL).
- Determine whether the current autonomy tier permits autonomous execution of
  a classified action or requires approval.
- Enumerate all rules deterministically; no LLM reasoning, no probability.

Design rules (from spec section 16.1 and 16.2):
- Autonomy tier is SEPARATE from action-risk.  Tier is a policy setting;
  risk is an intrinsic property of the action.
- Fail-closed: any unrecognised action_name defaults to CRITICAL risk.
- Policy misconfiguration raises PolicyConfigurationError, not a silent deny.
"""

from dataclasses import dataclass
from enum import Enum, unique
from typing import Dict, Optional, Tuple

from src.trust.capability import ActionRisk
from src.trust.errors import PolicyConfigurationError

# ---------------------------------------------------------------------------
# Autonomy tiers (operational policy configuration, NOT risk classification)
# ---------------------------------------------------------------------------


@unique
class AutonomyTier(int, Enum):
    """Operational autonomy tier.

    Tier 0 -- Strict Manual Approval:  every mutation requires explicit user
              confirmation.
    Tier 1 -- Safe / Bounded Execution: read-only ops autonomous; mutations
              require approval.
    Tier 2 -- Supervised Autonomy: routine pre-approved ops autonomous;
              high-impact or external requires approval.
    Tier 3 -- Full Bounded Task Autonomy: bounded autonomous execution within
              approved workflow scope; fail-closed triggers remain active.
    """

    STRICT_MANUAL = 0
    SAFE_BOUNDED = 1
    SUPERVISED = 2
    FULL_BOUNDED = 3


# ---------------------------------------------------------------------------
# Policy decision outcome
# ---------------------------------------------------------------------------


@unique
class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"  # Proceed autonomously
    ESCALATE = "ESCALATE"  # Requires user approval before proceeding
    DENY = "DENY"  # Hard deny; no approval pathway


@dataclass(frozen=True)
class PolicyResult:
    """The deterministic output of one policy evaluation."""

    decision: PolicyDecision
    action_name: str
    risk: ActionRisk
    autonomy_tier: AutonomyTier
    rule_id: str  # Opaque rule reference for audit log
    rationale: str  # Human-readable explanation (no secrets)


# ---------------------------------------------------------------------------
# Canonical action-risk registry
# ---------------------------------------------------------------------------

# Mapping: action_name_prefix -> ActionRisk
# Longest-prefix match is used; unrecognised => CRITICAL (fail-closed).
_ACTION_RISK_TABLE: Dict[str, ActionRisk] = {
    # Read-only filesystem
    "filesystem.read": ActionRisk.LOW,
    "filesystem.stat": ActionRisk.LOW,
    "filesystem.list": ActionRisk.LOW,
    # Local state mutations (reversible)
    "filesystem.write": ActionRisk.MEDIUM,
    "filesystem.create": ActionRisk.MEDIUM,
    "filesystem.rename": ActionRisk.MEDIUM,
    # Destructive filesystem
    "filesystem.delete": ActionRisk.HIGH,
    "filesystem.purge": ActionRisk.CRITICAL,
    # Process / shell
    "process.read": ActionRisk.LOW,
    "process.run_test": ActionRisk.MEDIUM,
    "process.run_lint": ActionRisk.MEDIUM,
    "process.run": ActionRisk.HIGH,
    "process.kill": ActionRisk.HIGH,
    # Memory / knowledge
    "memory.read": ActionRisk.LOW,
    "memory.write": ActionRisk.MEDIUM,
    "memory.delete": ActionRisk.HIGH,
    # Network
    "network.read": ActionRisk.HIGH,
    "network.write": ActionRisk.CRITICAL,
    # Credential
    "credential.read": ActionRisk.CRITICAL,
    "credential.write": ActionRisk.CRITICAL,
    # Settings / policy
    "settings.read": ActionRisk.LOW,
    "settings.write": ActionRisk.HIGH,
    "policy.change": ActionRisk.CRITICAL,
    # Project lifecycle
    "project.create": ActionRisk.MEDIUM,
    "project.update": ActionRisk.MEDIUM,
    "project.delete": ActionRisk.CRITICAL,
    "project.purge": ActionRisk.CRITICAL,
    # Workflow
    "workflow.read": ActionRisk.LOW,
    "workflow.advance": ActionRisk.MEDIUM,
    "workflow.create": ActionRisk.MEDIUM,
    "workflow.delete": ActionRisk.HIGH,
    # Fast Decision Engine (advisory only, never executes)
    "decision.evaluate": ActionRisk.LOW,
    # Audit (read-only access to audit log)
    "audit.read": ActionRisk.LOW,
}

# Tier → maximum autonomous (non-escalated) risk level
# Actions with risk ABOVE this level require escalation at this tier.
_TIER_MAX_AUTONOMOUS_RISK: Dict[AutonomyTier, ActionRisk] = {
    AutonomyTier.STRICT_MANUAL: ActionRisk.LOW,  # Only LOW can be autonomous
    # Tier 0 note: even LOW actions *should* require approval in strict mode;
    # we allow LOW to proceed to avoid blocking pure diagnostics, but callers
    # MUST consult user for everything else.
    AutonomyTier.SAFE_BOUNDED: ActionRisk.LOW,  # Read-only autonomous
    AutonomyTier.SUPERVISED: ActionRisk.MEDIUM,  # Routine mutations autonomous
    AutonomyTier.FULL_BOUNDED: ActionRisk.HIGH,  # Bounded; CRITICAL still escalates
}

# Risk ordering for comparison
_RISK_ORDER: Dict[ActionRisk, int] = {
    ActionRisk.LOW: 0,
    ActionRisk.MEDIUM: 1,
    ActionRisk.HIGH: 2,
    ActionRisk.CRITICAL: 3,
}


def _classify_risk(action_name: str) -> Tuple[ActionRisk, str]:
    """Return (ActionRisk, rule_id) for the given action name.

    Uses longest-prefix match against the canonical risk table.
    Unrecognised actions default to CRITICAL (fail-closed).
    """
    best_prefix = ""
    best_risk = ActionRisk.CRITICAL
    rule_id = "RULE:UNRECOGNISED_ACTION"

    for prefix, risk in _ACTION_RISK_TABLE.items():
        if action_name == prefix or action_name.startswith(prefix + "."):
            if len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_risk = risk
                rule_id = f"RULE:{prefix.upper().replace('.', '_')}"

    # Exact match wins over prefix
    if action_name in _ACTION_RISK_TABLE:
        best_risk = _ACTION_RISK_TABLE[action_name]
        rule_id = f"RULE:{action_name.upper().replace('.', '_')}"

    return best_risk, rule_id


class PolicyEngine:
    """Deterministic policy evaluator.

    Thread-safe: all state is read-only after construction; no mutable shared
    state beyond the autonomy tier, which is only changed by explicit
    administrator action and protected by a caller lock.
    """

    def __init__(
        self,
        autonomy_tier: AutonomyTier = AutonomyTier.STRICT_MANUAL,
        hard_denied_actions: Optional[frozenset] = None,
    ) -> None:
        """Initialise the policy engine.

        Args:
            autonomy_tier:        Current operational autonomy tier.
            hard_denied_actions:  Set of action_name strings that are always
                                  DENY regardless of tier or approval.
        """
        if not isinstance(autonomy_tier, AutonomyTier):
            raise PolicyConfigurationError(
                f"Invalid autonomy tier {autonomy_tier!r}; must be an AutonomyTier enum member."
            )
        self._tier = autonomy_tier
        # Actions that can NEVER be approved -- permanent DENY
        self._hard_denied: frozenset = hard_denied_actions or frozenset(
            {
                "policy.change",  # No LLM/skill may change Trust Core policy
            }
        )

    @property
    def autonomy_tier(self) -> AutonomyTier:
        return self._tier

    def set_autonomy_tier(self, tier: AutonomyTier) -> None:
        """Update the operational autonomy tier.

        This is a privileged operation; callers must log this change via the
        audit subsystem before calling this method.

        Raises:
            PolicyConfigurationError: if tier is not a valid AutonomyTier.
        """
        if not isinstance(tier, AutonomyTier):
            raise PolicyConfigurationError(
                f"Invalid autonomy tier {tier!r}; must be an AutonomyTier enum member."
            )
        self._tier = tier

    def evaluate(self, action_name: str, project_id: Optional[str] = None) -> PolicyResult:
        """Evaluate whether *action_name* is permitted under the current policy.

        Args:
            action_name:  Canonical action name (e.g. "filesystem.write").
            project_id:   Optional project context for scope reasoning.

        Returns:
            PolicyResult with ALLOW, ESCALATE, or DENY decision.

        This method NEVER raises; it always returns a PolicyResult.  The
        caller is responsible for acting on the decision.
        """
        risk, rule_id = _classify_risk(action_name)

        # Hard-denied actions: unconditional DENY
        if action_name in self._hard_denied:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                action_name=action_name,
                risk=risk,
                autonomy_tier=self._tier,
                rule_id=rule_id,
                rationale=(
                    f"Action '{action_name}' is on the hard-deny list; no approval pathway exists."
                ),
            )

        # Tier 0: only LOW actions may be autonomous; everything else escalates
        if self._tier == AutonomyTier.STRICT_MANUAL:
            if _RISK_ORDER[risk] > _RISK_ORDER[ActionRisk.LOW]:
                return PolicyResult(
                    decision=PolicyDecision.ESCALATE,
                    action_name=action_name,
                    risk=risk,
                    autonomy_tier=self._tier,
                    rule_id=rule_id,
                    rationale=(
                        f"Autonomy tier STRICT_MANUAL: action '{action_name}' "
                        f"(risk={risk.value}) requires explicit user approval."
                    ),
                )

        else:
            max_autonomous_risk = _TIER_MAX_AUTONOMOUS_RISK[self._tier]
            if _RISK_ORDER[risk] > _RISK_ORDER[max_autonomous_risk]:
                return PolicyResult(
                    decision=PolicyDecision.ESCALATE,
                    action_name=action_name,
                    risk=risk,
                    autonomy_tier=self._tier,
                    rule_id=rule_id,
                    rationale=(
                        f"Autonomy tier {self._tier.name}: action '{action_name}' "
                        f"(risk={risk.value}) exceeds autonomous limit "
                        f"(max={max_autonomous_risk.value}). Approval required."
                    ),
                )

        return PolicyResult(
            decision=PolicyDecision.ALLOW,
            action_name=action_name,
            risk=risk,
            autonomy_tier=self._tier,
            rule_id=rule_id,
            rationale=(
                f"Autonomy tier {self._tier.name}: action '{action_name}' "
                f"(risk={risk.value}) permitted autonomously."
            ),
        )
