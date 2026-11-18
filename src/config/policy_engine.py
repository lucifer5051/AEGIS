"""SAGE Central Policy Evaluation Engine.

Canonical Reference:
    PROMPT 069: SAGE Configuration & Policy System
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md

Evaluation Pipeline:
    Action Request → Effective Configuration → TrustCore Evaluation → Policy Evaluation → Approval Decision

Invariants:
1. TrustCore Authorization is authoritative and unbypassable.
2. Configuration can only narrow/restrict permissions, NEVER elevate privileges or mint capabilities.
3. Every decision produces a structured, explainable result with policy references.
4. Fail-closed: any evaluation error results in DENY.
5. Zero secrets: no tokens, passwords, or keys in explanation strings or audit trails.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config.hierarchy import ConfigurationHierarchyResolver
from src.config.models import AutonomyTier, NetworkMode
from src.config.policy_models import (
    AutonomyMode,
    AutonomyProfile,
    PolicyEvaluationDecision,
    PolicyEvaluationDecisionType,
    PolicyEvaluationRequest,
    ResolvedConfiguration,
    SecurityPolicy,
)
from src.trust.audit import _check_no_secrets
from src.trust.capability import ActionRisk
from src.trust.policy import PolicyEngine as TrustCorePolicyEngine

logger = logging.getLogger(__name__)


class CentralPolicyEvaluationEngine:
    """Central policy evaluation engine reconciling SecurityPolicy, AutonomyProfile, and TrustCore."""

    def __init__(
        self,
        hierarchy_resolver: Optional[ConfigurationHierarchyResolver] = None,
        trustcore_policy: Optional[TrustCorePolicyEngine] = None,
    ) -> None:
        self._resolver = hierarchy_resolver or ConfigurationHierarchyResolver()
        self._trustcore = trustcore_policy or TrustCorePolicyEngine()

    @property
    def hierarchy_resolver(self) -> ConfigurationHierarchyResolver:
        return self._resolver

    def evaluate_action(
        self,
        request: PolicyEvaluationRequest,
        resolved_config: Optional[ResolvedConfiguration] = None,
    ) -> PolicyEvaluationDecision:
        """Evaluate whether a requested action is ALLOW, DENY, REQUIRE_APPROVAL, or DEFER.

        Deterministic evaluation steps:
        1. Sanitize request inputs for secret leaks.
        2. Resolve effective configuration (SecurityPolicy + AutonomyProfile).
        3. Check SecurityPolicy hard boundaries (Paths, Domains, Tools, Commands).
        4. Check TrustCore deterministic risk & hard-deny classification.
        5. Check AutonomyMode constraints & approval thresholds.
        6. Return structured decision.
        """
        try:
            # 1. Zero secret leak check
            _check_no_secrets(request.action_name)
            if request.target_resource:
                _check_no_secrets(request.target_resource)
            if request.arguments:
                _check_no_secrets(str(request.arguments))

            # 2. Get effective configuration
            config = resolved_config or self._resolver.resolve(active_mode_name=request.active_mode)
            sec: SecurityPolicy = config.security_policy
            aut: AutonomyProfile = config.autonomy_profile

            rule_refs: List[str] = []

            # -----------------------------------------------------------------
            # 3. SecurityPolicy Boundary Checks (Can only restrict/deny)
            # -----------------------------------------------------------------

            # Tool Denylist check
            if request.tool_name:
                if request.tool_name in sec.tools.denied_tools:
                    return PolicyEvaluationDecision(
                        decision=PolicyEvaluationDecisionType.DENY,
                        action_name=request.action_name,
                        reason=f"Tool '{request.tool_name}' is explicitly denied by SecurityPolicy.tools.denied_tools.",
                        rule_references=["RULE:SEC_TOOL_DENIED"],
                        governing_layer="SecurityPolicy.ToolPolicy",
                        autonomy_mode=aut.mode.value,
                        approval_required=False,
                    )
                if sec.tools.allowed_tools is not None and request.tool_name not in sec.tools.allowed_tools:
                    return PolicyEvaluationDecision(
                        decision=PolicyEvaluationDecisionType.DENY,
                        action_name=request.action_name,
                        reason=f"Tool '{request.tool_name}' is not in SecurityPolicy.tools.allowed_tools whitelist.",
                        rule_references=["RULE:SEC_TOOL_NOT_ALLOWED"],
                        governing_layer="SecurityPolicy.ToolPolicy",
                        autonomy_mode=aut.mode.value,
                        approval_required=False,
                    )

            # Filesystem path checks
            if request.target_resource:
                target_str = str(request.target_resource).replace("\\", "/")

                # Check restricted system resources
                for rest in sec.restricted_resources:
                    if rest.replace("\\", "/").lower() in target_str.lower():
                        return PolicyEvaluationDecision(
                            decision=PolicyEvaluationDecisionType.DENY,
                            action_name=request.action_name,
                            reason=f"Target resource '{request.target_resource}' is a restricted system resource.",
                            rule_references=["RULE:SEC_RESTRICTED_RESOURCE"],
                            governing_layer="SecurityPolicy.RestrictedResources",
                            autonomy_mode=aut.mode.value,
                            approval_required=False,
                        )

                # Check denied paths
                for denied in sec.filesystem.denied_paths:
                    if denied.replace("\\", "/").lower() in target_str.lower():
                        return PolicyEvaluationDecision(
                            decision=PolicyEvaluationDecisionType.DENY,
                            action_name=request.action_name,
                            reason=f"Access to path '{request.target_resource}' is denied by SecurityPolicy.filesystem.denied_paths.",
                            rule_references=["RULE:SEC_PATH_DENIED"],
                            governing_layer="SecurityPolicy.FilesystemPolicy",
                            autonomy_mode=aut.mode.value,
                            approval_required=False,
                        )

                # Check read-only paths on mutation actions
                if any(m in request.action_name for m in ("write", "delete", "create", "modify")):
                    for ro_path in sec.filesystem.read_only_paths:
                        if ro_path.replace("\\", "/").lower() in target_str.lower():
                            return PolicyEvaluationDecision(
                                decision=PolicyEvaluationDecisionType.DENY,
                                action_name=request.action_name,
                                reason=f"Path '{request.target_resource}' is configured as read-only.",
                                rule_references=["RULE:SEC_PATH_READ_ONLY"],
                                governing_layer="SecurityPolicy.FilesystemPolicy",
                                autonomy_mode=aut.mode.value,
                                approval_required=False,
                            )

            # Shell execution policy checks
            if "shell" in request.action_name:
                if not sec.shell.enabled:
                    return PolicyEvaluationDecision(
                        decision=PolicyEvaluationDecisionType.DENY,
                        action_name=request.action_name,
                        reason="Shell execution is disabled by SecurityPolicy.shell.enabled.",
                        rule_references=["RULE:SEC_SHELL_DISABLED"],
                        governing_layer="SecurityPolicy.ShellPolicy",
                        autonomy_mode=aut.mode.value,
                        approval_required=False,
                    )
                cmd = str(request.arguments.get("command", ""))
                for denied_cmd in sec.shell.denied_commands:
                    if denied_cmd.lower() in cmd.lower():
                        return PolicyEvaluationDecision(
                            decision=PolicyEvaluationDecisionType.DENY,
                            action_name=request.action_name,
                            reason=f"Command matched forbidden shell command pattern '{denied_cmd}'.",
                            rule_references=["RULE:SEC_COMMAND_FORBIDDEN"],
                            governing_layer="SecurityPolicy.ShellPolicy",
                            autonomy_mode=aut.mode.value,
                            approval_required=False,
                        )

            # Network policy checks
            if "network" in request.action_name or "egress" in request.action_name:
                if sec.network.mode == NetworkMode.LOCAL_ONLY:
                    return PolicyEvaluationDecision(
                        decision=PolicyEvaluationDecisionType.DENY,
                        action_name=request.action_name,
                        reason="Network egress is blocked: SecurityPolicy.network.mode is LOCAL_ONLY.",
                        rule_references=["RULE:SEC_NETWORK_LOCAL_ONLY"],
                        governing_layer="SecurityPolicy.NetworkPolicy",
                        autonomy_mode=aut.mode.value,
                        approval_required=False,
                    )

            # -----------------------------------------------------------------
            # 4. TrustCore Policy Evaluation
            # -----------------------------------------------------------------
            tc_res = self._trustcore.evaluate(request.action_name, project_id=request.project_id)
            action_risk_str = tc_res.risk.value if hasattr(tc_res.risk, "value") else str(tc_res.risk)
            rule_refs.append(f"TRUSTCORE:{tc_res.rule_id}")

            if tc_res.decision.value == "DENY":
                return PolicyEvaluationDecision(
                    decision=PolicyEvaluationDecisionType.DENY,
                    action_name=request.action_name,
                    reason=f"TrustCore hard-denied action: {tc_res.rationale}",
                    rule_references=rule_refs,
                    action_risk=action_risk_str,
                    governing_layer="TrustCore.PolicyEngine",
                    autonomy_mode=aut.mode.value,
                    approval_required=False,
                )

            # -----------------------------------------------------------------
            # 5. Autonomy Mode & Approval Gates Evaluation
            # -----------------------------------------------------------------

            # Check mandatory approval actions
            if request.action_name in sec.approval.mandatory_approval_actions:
                rule_refs.append("RULE:SEC_MANDATORY_APPROVAL")
                return PolicyEvaluationDecision(
                    decision=PolicyEvaluationDecisionType.REQUIRE_APPROVAL,
                    action_name=request.action_name,
                    reason=f"Action '{request.action_name}' is classified as a mandatory approval operation.",
                    rule_references=rule_refs,
                    action_risk=action_risk_str,
                    governing_layer="SecurityPolicy.ApprovalPolicy",
                    autonomy_mode=aut.mode.value,
                    approval_required=True,
                )

            # Manual mode: propose-only, consequential actions require approval
            if aut.mode == AutonomyMode.MANUAL or aut.propose_only:
                rule_refs.append("RULE:AUTONOMY_MANUAL_PROPOSE_ONLY")
                return PolicyEvaluationDecision(
                    decision=PolicyEvaluationDecisionType.REQUIRE_APPROVAL,
                    action_name=request.action_name,
                    reason="Autonomy Mode is MANUAL (propose-only): all actions require user approval.",
                    rule_references=rule_refs,
                    action_risk=action_risk_str,
                    governing_layer="AutonomyProfile.Manual",
                    autonomy_mode=aut.mode.value,
                    approval_required=True,
                )

            # Check explicit approval actions under profile
            if request.action_name in aut.require_approval_for_actions or "*" in aut.require_approval_for_actions:
                rule_refs.append("RULE:AUTONOMY_PROFILE_REQUIRE_APPROVAL")
                return PolicyEvaluationDecision(
                    decision=PolicyEvaluationDecisionType.REQUIRE_APPROVAL,
                    action_name=request.action_name,
                    reason=f"Autonomy profile '{aut.name}' requires explicit approval for action '{request.action_name}'.",
                    rule_references=rule_refs,
                    action_risk=action_risk_str,
                    governing_layer="AutonomyProfile",
                    autonomy_mode=aut.mode.value,
                    approval_required=True,
                )

            # Check if action risk is allowed autonomously under this profile
            if action_risk_str.upper() not in [r.upper() for r in aut.allowed_autonomous_risks]:
                rule_refs.append("RULE:AUTONOMY_RISK_THRESHOLD_EXCEEDED")
                return PolicyEvaluationDecision(
                    decision=PolicyEvaluationDecisionType.REQUIRE_APPROVAL,
                    action_name=request.action_name,
                    reason=(
                        f"Action risk '{action_risk_str}' exceeds permitted autonomous risks "
                        f"{aut.allowed_autonomous_risks} under mode '{aut.mode.value}'."
                    ),
                    rule_references=rule_refs,
                    action_risk=action_risk_str,
                    governing_layer="AutonomyProfile.TierBoundary",
                    autonomy_mode=aut.mode.value,
                    approval_required=True,
                )

            # If TrustCore required escalation/approval
            if tc_res.decision.value == "ESCALATE":
                rule_refs.append("RULE:TRUSTCORE_ESCALATE")
                return PolicyEvaluationDecision(
                    decision=PolicyEvaluationDecisionType.REQUIRE_APPROVAL,
                    action_name=request.action_name,
                    reason=tc_res.rationale,
                    rule_references=rule_refs,
                    action_risk=action_risk_str,
                    governing_layer="TrustCore.PolicyEngine",
                    autonomy_mode=aut.mode.value,
                    approval_required=True,
                )

            # -----------------------------------------------------------------
            # 6. Permitted: ALLOW
            # -----------------------------------------------------------------
            rule_refs.append("RULE:POLICY_PERMITTED")
            return PolicyEvaluationDecision(
                decision=PolicyEvaluationDecisionType.ALLOW,
                action_name=request.action_name,
                reason=f"Action '{request.action_name}' is authorized under mode '{aut.mode.value}' (Risk: {action_risk_str}).",
                rule_references=rule_refs,
                action_risk=action_risk_str,
                governing_layer="CentralPolicyEngine",
                autonomy_mode=aut.mode.value,
                approval_required=False,
            )

        except Exception as exc:
            logger.error("Policy evaluation failure (failing closed): %s", exc, exc_info=True)
            return PolicyEvaluationDecision(
                decision=PolicyEvaluationDecisionType.DENY,
                action_name=request.action_name,
                reason=f"Fail-closed: Policy evaluation encountered an unexpected error: {type(exc).__name__}",
                rule_references=["RULE:FAIL_CLOSED_ERROR"],
                governing_layer="CentralPolicyEngine.FailClosed",
                approval_required=False,
            )

    def explain_decision(self, request: PolicyEvaluationRequest) -> str:
        """Produce an explainable, human-readable breakdown of a policy decision."""
        decision = self.evaluate_action(request)
        config = self._resolver.resolve(active_mode_name=request.active_mode)

        lines = [
            "=== SAGE POLICY EVALUATION EXPLANATION ===",
            f"Action Name:         {request.action_name}",
            f"Target Resource:     {request.target_resource or 'N/A'}",
            f"Active Mode:         {config.active_mode or 'Default'}",
            f"Autonomy Mode:       {config.autonomy_profile.mode.value} (Tier {config.autonomy_profile.tier.value})",
            f"Security Policy:     {config.security_policy.name}",
            f"Decision:            {decision.decision.value}",
            f"Action Risk:         {decision.action_risk}",
            f"Approval Required:   {decision.approval_required}",
            f"Governing Layer:     {decision.governing_layer}",
            f"Reason:              {decision.reason}",
            f"Rule References:     {', '.join(decision.rule_references)}",
        ]
        return "\n".join(lines)
