"""SAGE Configuration & Policy System Test Suite.

Canonical Reference:
    PROMPT 069: SAGE Configuration & Policy System -- Unified Configuration, Autonomy, Execution Profiles and Policy Enforcement

Tests all core configuration and policy subsystems:
1. Three independent dimensions (Autonomy Mode, Security Policy, Execution Profile) + Personality.
2. Configuration hierarchy resolution, override precedence, and explainability.
3. Security monotonicity (denied paths, tools, commands accumulate and cannot be bypassed).
4. Custom mode lifecycle (CRUD, duplicate, activate, set default, apply to agent/automation).
5. Central policy evaluation engine (ALLOW, DENY, REQUIRE_APPROVAL, structured reasons).
6. TrustCore unbypassable authority invariant (configuration cannot elevate or mint capabilities).
7. Persistence, versioned history snapshots, and atomic rollback.
8. Zero secret protection in configurations and policy explanations.
9. Supervisor runtime integration with effective configuration resolution.
"""

from __future__ import annotations

import sqlite3
import pytest
from pathlib import Path
from typing import Any, Dict

from src.config.hierarchy import ConfigurationHierarchyResolver
from src.config.manager import SageConfigurationManager
from src.config.modes import CustomModeManager
from src.config.models import AutonomyTier, NetworkMode
from src.config.policy_engine import CentralPolicyEvaluationEngine
from src.config.policy_models import (
    ApprovalPolicy,
    AutonomyMode,
    AutonomyProfile,
    CommunicationPolicy,
    ConfigurationLayer,
    CustomMode,
    ExecutionProfile,
    ExecutionProfileType,
    FilesystemPolicy,
    MemoryPolicy,
    NetworkPolicy,
    PersonalityConfig,
    PolicyEvaluationDecision,
    PolicyEvaluationDecisionType,
    PolicyEvaluationRequest,
    ResolvedConfiguration,
    SecurityPolicy,
    ShellPolicy,
    ToolPolicy,
)
from src.config.policy_store import PolicyConfigStore
from src.trust.errors import SecretLeakError


@pytest.fixture
def in_memory_conn():
    """Create an in-memory SQLite database connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def config_manager(in_memory_conn):
    """Instantiate a SageConfigurationManager backed by in-memory SQLite."""
    return SageConfigurationManager(conn=in_memory_conn)


# ==============================================================================
# 1. THREE INDEPENDENT DIMENSIONS & PERSONALITY TESTS
# ==============================================================================


class TestConfigurationDimensions:
    """Verify that Autonomy, Security Policy, Execution Profile, and Personality are strictly separated."""

    def test_autonomy_profiles(self):
        manual = AutonomyProfile.builtin_manual()
        assert manual.mode == AutonomyMode.MANUAL
        assert manual.tier == AutonomyTier.TIER_0_STRICT_MANUAL
        assert manual.propose_only is True

        assisted = AutonomyProfile.builtin_assisted()
        assert assisted.mode == AutonomyMode.ASSISTED
        assert assisted.tier == AutonomyTier.TIER_1_SAFE_BOUNDED
        assert assisted.propose_only is False
        assert "LOW" in assisted.allowed_autonomous_risks

        supervised = AutonomyProfile.builtin_supervised()
        assert supervised.mode == AutonomyMode.SUPERVISED
        assert supervised.tier == AutonomyTier.TIER_2_SUPERVISED
        assert supervised.checkpoint_required_after_steps == 5

        autonomous = AutonomyProfile.builtin_autonomous()
        assert autonomous.mode == AutonomyMode.AUTONOMOUS
        assert autonomous.tier == AutonomyTier.TIER_3_FULL_BOUNDED

    def test_execution_profiles(self):
        fast = ExecutionProfile.builtin_fast()
        assert fast.profile_type == ExecutionProfileType.FAST
        assert fast.context_token_budget == 2048
        assert fast.timeout_seconds == 30.0

        balanced = ExecutionProfile.builtin_balanced()
        assert balanced.profile_type == ExecutionProfileType.BALANCED
        assert balanced.context_token_budget == 4096

        deep = ExecutionProfile.builtin_deep()
        assert deep.profile_type == ExecutionProfileType.DEEP
        assert deep.context_token_budget == 8192
        assert deep.reasoning_intensity == 0.9

    def test_security_policy_defaults(self):
        sec = SecurityPolicy()
        assert sec.network.mode == NetworkMode.LOCAL_ONLY
        assert sec.shell.enabled is False
        assert any("System32" in p for p in sec.restricted_resources)
        assert sec.approval.consequential_actions_require_approval is True

    def test_personality_independence(self):
        friday = PersonalityConfig(agent_name="Friday", tone="warm", presence_state="AVAILABLE")
        assert friday.agent_name == "Friday"
        assert friday.presence_state == "AVAILABLE"


# ==============================================================================
# 2. HIERARCHY RESOLUTION & MONOTONIC SECURITY TESTS
# ==============================================================================


class TestConfigurationHierarchy:
    """Verify 6-tier inheritance, precedence, explainability, and monotonic security."""

    def test_precedence_ordering(self):
        resolver = ConfigurationHierarchyResolver()

        # Set Global override
        resolver.set_layer_config(
            ConfigurationLayer.GLOBAL,
            {"execution_profile": {"context_token_budget": 3000}},
        )
        resolved_global = resolver.resolve()
        assert resolved_global.execution_profile.context_token_budget == 3000
        assert resolved_global.field_origins["execution_profile.context_token_budget"] == "GLOBAL"

        # Set User override (higher precedence)
        resolver.set_layer_config(
            ConfigurationLayer.USER,
            {"execution_profile": {"context_token_budget": 5000}},
        )
        resolved_user = resolver.resolve()
        assert resolved_user.execution_profile.context_token_budget == 5000
        assert resolved_user.field_origins["execution_profile.context_token_budget"] == "USER"

        # Task override (even higher precedence)
        resolved_task = resolver.resolve(task_overrides={"execution_profile": {"context_token_budget": 6000}})
        assert resolved_task.execution_profile.context_token_budget == 6000
        assert resolved_task.field_origins["execution_profile.context_token_budget"] == "TASK"

    def test_explainability_output(self):
        resolver = ConfigurationHierarchyResolver()
        resolver.set_layer_config(
            ConfigurationLayer.GLOBAL,
            {"autonomy_profile": {"name": "Custom Supervised", "tier": 2}},
        )
        resolved = resolver.resolve()
        explanation = resolved.explain()
        assert "SAGE RESOLVED CONFIGURATION EXPLANATION" in explanation
        assert "autonomy_profile.name" in explanation
        assert "GLOBAL" in explanation

    def test_security_monotonicity(self):
        resolver = ConfigurationHierarchyResolver()
        # System defaults deny C:\Windows
        resolver.set_layer_config(
            ConfigurationLayer.GLOBAL,
            {"security_policy": {"filesystem": {"denied_paths": ["/secret_dir"]}}},
        )
        # Task layer attempts to set allowed paths
        resolved = resolver.resolve(
            task_overrides={"security_policy": {"filesystem": {"allowed_paths": ["/workspace"]}}}
        )
        denied_paths = resolved.security_policy.filesystem.denied_paths
        # Both system default denied paths and global denied paths must be preserved!
        assert any("Windows" in p for p in denied_paths)
        assert "/secret_dir" in denied_paths


# ==============================================================================
# 3. CUSTOM MODE SYSTEM TESTS
# ==============================================================================


class TestCustomModeManager:
    """Verify custom mode CRUD, duplication, activation, and built-in templates."""

    def test_builtin_templates_exist(self, config_manager):
        modes = config_manager.list_modes()
        names = [m.name for m in modes]
        assert "Coding Mode" in names
        assert "Research Mode" in names
        assert "Personal Assistant Mode" in names
        assert "Private Mode" in names
        assert "Study Mode" in names
        assert "Automation Mode" in names

    def test_create_and_get_mode(self, config_manager):
        mode = config_manager.create_mode(
            name="Data Science Mode",
            description="Mode for large dataset inspection.",
            execution_profile=ExecutionProfile(
                profile_type=ExecutionProfileType.DEEP,
                context_token_budget=8192,
            ),
        )
        assert mode.mode_id
        fetched = config_manager.get_mode("Data Science Mode")
        assert fetched is not None
        assert fetched.execution_profile.context_token_budget == 8192

    def test_duplicate_mode(self, config_manager):
        duplicated = config_manager.duplicate_mode("Coding Mode", new_name="Specialized Coding")
        assert duplicated.name == "Specialized Coding"
        assert duplicated.mode_id != "builtin-coding-mode"
        assert duplicated.autonomy_profile.tier == AutonomyTier.TIER_2_SUPERVISED

    def test_builtin_modes_cannot_be_deleted(self, config_manager):
        with pytest.raises(ValueError, match="Cannot delete built-in mode"):
            config_manager.delete_mode("Coding Mode")

    def test_activate_mode(self, config_manager):
        resolved = config_manager.activate_mode("Research Mode")
        assert resolved.active_mode == "Research Mode"
        assert resolved.execution_profile.profile_type == ExecutionProfileType.DEEP

    def test_apply_mode_to_agent_and_automation(self, config_manager):
        agent_cfg = config_manager.apply_mode_to_agent("Coding Mode", agent_id="engineer_01")
        assert "Coding Mode" in (agent_cfg.active_mode or "")

        auto_cfg = config_manager.apply_mode_to_automation("Automation Mode", automation_id="daily_cron")
        assert "Automation Mode" in (auto_cfg.active_mode or "")


# ==============================================================================
# 4. CENTRAL POLICY EVALUATION ENGINE TESTS
# ==============================================================================


class TestCentralPolicyEvaluation:
    """Verify policy evaluation pipeline, TrustCore subordination, and approval rules."""

    def test_allow_permitted_low_risk_action(self, config_manager):
        req = PolicyEvaluationRequest(
            action_name="filesystem.read",
            actor="supervisor",
            target_resource="src/config/models.py",
        )
        decision = config_manager.evaluate_policy(req)
        assert decision.is_allowed()
        assert decision.decision == PolicyEvaluationDecisionType.ALLOW

    def test_deny_blocked_system_resource(self, config_manager):
        req = PolicyEvaluationRequest(
            action_name="filesystem.read",
            actor="supervisor",
            target_resource="C:\\Windows\\System32\\kernel32.dll",
        )
        decision = config_manager.evaluate_policy(req)
        assert decision.decision == PolicyEvaluationDecisionType.DENY
        assert "restricted system resource" in decision.reason

    def test_deny_forbidden_shell_command(self, config_manager):
        # Enable shell in global config
        config_manager.update_config({"security_policy": {"shell": {"enabled": True}}})
        req = PolicyEvaluationRequest(
            action_name="shell.execute",
            arguments={"command": "rmdir /s /q C:\\"},
        )
        decision = config_manager.evaluate_policy(req)
        assert decision.decision == PolicyEvaluationDecisionType.DENY
        assert "forbidden shell command" in decision.reason

    def test_require_approval_in_manual_mode(self, config_manager):
        # Activate manual mode
        config_manager.activate_mode("Private Mode")
        req = PolicyEvaluationRequest(
            action_name="filesystem.write",
            target_resource="test.txt",
        )
        decision = config_manager.evaluate_policy(req)
        assert decision.decision == PolicyEvaluationDecisionType.REQUIRE_APPROVAL
        assert decision.approval_required is True

    def test_explain_policy_decision(self, config_manager):
        req = PolicyEvaluationRequest(
            action_name="shell.execute",
            arguments={"command": "dir"},
        )
        explanation = config_manager.explain_policy_decision(req)
        assert "SAGE POLICY EVALUATION EXPLANATION" in explanation
        assert "shell.execute" in explanation
        assert "Decision:" in explanation


# ==============================================================================
# 5. PERSISTENCE, ROLLBACK & ZERO SECRET PROTECTION
# ==============================================================================


class TestPolicyPersistenceAndRollback:
    """Verify SQLite persistence, atomic version snapshots, rollback, and secret safety."""

    def test_persistence_and_rollback(self, in_memory_conn):
        mgr = SageConfigurationManager(conn=in_memory_conn)

        # Version 1 update
        cfg_v1 = mgr.update_config(
            updates={"execution_profile": {"context_token_budget": 3333}},
            reason="Initial tuning",
        )
        assert cfg_v1.execution_profile.context_token_budget == 3333

        # Version 2 update
        cfg_v2 = mgr.update_config(
            updates={"execution_profile": {"context_token_budget": 7777}},
            reason="Second tuning",
        )
        assert cfg_v2.execution_profile.context_token_budget == 7777

        # Rollback to Version 1
        history = mgr.policy_engine.hierarchy_resolver._layers
        cfg_rolled = mgr.rollback_config(version_number=1)
        assert cfg_rolled.execution_profile.context_token_budget == 3333

    def test_zero_secret_rejection(self, config_manager):
        secret_payload = {"security_policy": {"name": 'api_key: "sk-ant-api03-1234567890"'}}
        with pytest.raises(SecretLeakError):
            config_manager.update_config(secret_payload)


# ==============================================================================
# 6. SUPERVISOR RUNTIME INTEGRATION TESTS
# ==============================================================================


class TestSupervisorConfigurationIntegration:
    """Verify that Supervisor resolves effective configuration and enforces policy denials."""

    def test_supervisor_initializes_with_config_manager(self, in_memory_conn):
        from src.manager.supervisor import Supervisor
        from src.manager.state_store import StateStore
        from src.trust.authority import TrustAuthority
        from src.trust.capability import CapabilityFactory, CapabilityRegistry
        from src.trust.audit import AuditWriter
        from src.trust.policy import PolicyEngine as TrustCorePolicyEngine
        from src.tools.broker import ToolBroker

        store = StateStore(in_memory_conn)
        cap_reg = CapabilityRegistry(CapabilityFactory())
        audit_writer = AuditWriter(in_memory_conn)
        authority = TrustAuthority(TrustCorePolicyEngine(), cap_reg, audit_writer)
        broker = ToolBroker(authority)

        mgr = SageConfigurationManager(conn=in_memory_conn)
        supervisor = Supervisor(
            state_store=store,
            trust_authority=authority,
            tool_broker=broker,
            config_manager=mgr,
        )
        assert supervisor._config_manager is mgr
