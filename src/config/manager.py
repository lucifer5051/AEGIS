"""SAGE Central Configuration & Policy Manager -- Unified Public API Surface.

Canonical Reference:
    PROMPT 069: SAGE Configuration & Policy System

Fulfills all centralized management and evaluation contracts:
- get_config() / update_config() / resolve_config() / get_effective_config()
- create_mode() / update_mode() / delete_mode() / activate_mode() / set_default_mode()
- evaluate_policy() / explain_policy_decision()
- validate_config() / rollback_config()
"""

from __future__ import annotations

import copy
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from src.config.hierarchy import ConfigurationHierarchyResolver
from src.config.modes import CustomModeManager
from src.config.policy_engine import CentralPolicyEvaluationEngine
from src.config.policy_models import (
    AutonomyProfile,
    ConfigurationLayer,
    CustomMode,
    ExecutionProfile,
    PersonalityConfig,
    PolicyEvaluationDecision,
    PolicyEvaluationRequest,
    ResolvedConfiguration,
    SecurityPolicy,
)
from src.config.policy_store import PolicyConfigStore
from src.trust.audit import _check_no_secrets
from src.trust.policy import PolicyEngine as TrustCorePolicyEngine

logger = logging.getLogger(__name__)


class SageConfigurationManager:
    """Central manager coordinating hierarchy resolution, mode lifecycle, persistence, and policy evaluation."""

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        trustcore_policy: Optional[TrustCorePolicyEngine] = None,
    ) -> None:
        self._conn = conn
        self._store = PolicyConfigStore(conn) if conn is not None else None
        self._modes = CustomModeManager(store=self._store)
        self._resolver = ConfigurationHierarchyResolver()
        self._policy_engine = CentralPolicyEvaluationEngine(
            hierarchy_resolver=self._resolver,
            trustcore_policy=trustcore_policy,
        )

        # Load persisted layers if store is available
        if self._store:
            for layer in ConfigurationLayer:
                if layer == ConfigurationLayer.SYSTEM_DEFAULTS:
                    continue
                persisted = self._store.load_layer_config(layer)
                if persisted:
                    self._resolver.set_layer_config(layer, persisted)

    @property
    def modes(self) -> CustomModeManager:
        return self._modes

    @property
    def resolver(self) -> ConfigurationHierarchyResolver:
        return self._resolver

    @property
    def policy_engine(self) -> CentralPolicyEvaluationEngine:
        return self._policy_engine

    # =========================================================================
    # CONFIGURATION QUERY & MUTATION
    # =========================================================================

    def get_config(self, layer: ConfigurationLayer = ConfigurationLayer.GLOBAL) -> Dict[str, Any]:
        """Retrieve raw configuration dictionary for a given layer."""
        return self._resolver.get_layer_config(layer)

    def update_config(
        self,
        updates: Dict[str, Any],
        layer: ConfigurationLayer = ConfigurationLayer.GLOBAL,
        reason: str = "Configuration update",
        actor: str = "user",
    ) -> ResolvedConfiguration:
        """Atomically validate, update, and persist configuration for a specific layer."""
        # 1. Enforce zero secrets
        _check_no_secrets(str(updates))
        _check_no_secrets(reason)

        # 2. Validate by test-merging and building models
        current = self._resolver.get_layer_config(layer)
        test_merged = copy.deepcopy(current)
        for k, v in updates.items():
            if isinstance(v, dict) and isinstance(test_merged.get(k), dict):
                test_merged[k].update(v)
            else:
                test_merged[k] = v

        # Validate structure
        self.validate_config(test_merged)

        # 3. Apply to in-memory hierarchy
        self._resolver.set_layer_config(layer, test_merged)

        # 4. Persist to SQLite if backed
        if self._store:
            self._store.save_layer_config(
                layer=layer,
                config_data=test_merged,
                reason=reason,
                actor=actor,
            )

        logger.info("Updated configuration for layer '%s' (reason=%s)", layer.value, reason)
        return self.get_effective_config()

    def resolve_config(
        self,
        task_overrides: Optional[Dict[str, Any]] = None,
        agent_overrides: Optional[Dict[str, Any]] = None,
        automation_overrides: Optional[Dict[str, Any]] = None,
        active_mode: Optional[str] = None,
    ) -> ResolvedConfiguration:
        """Resolve effective configuration across the hierarchy."""
        return self._resolver.resolve(
            task_overrides=task_overrides,
            agent_overrides=agent_overrides,
            automation_overrides=automation_overrides,
            active_mode_name=active_mode or (self._modes.get_active_mode().name if self._modes.get_active_mode() else None),
        )

    def get_effective_config(self) -> ResolvedConfiguration:
        """Get currently active effective configuration."""
        active = self._modes.get_active_mode()
        active_name = active.name if active else None
        return self._resolver.resolve(active_mode_name=active_name)

    def validate_config(self, config_dict: Dict[str, Any]) -> bool:
        """Validate an arbitrary configuration dictionary against schemas."""
        if "autonomy_profile" in config_dict:
            AutonomyProfile.model_validate(config_dict["autonomy_profile"])
        if "security_policy" in config_dict:
            SecurityPolicy.model_validate(config_dict["security_policy"])
        if "execution_profile" in config_dict:
            ExecutionProfile.model_validate(config_dict["execution_profile"])
        if "personality_config" in config_dict:
            PersonalityConfig.model_validate(config_dict["personality_config"])
        return True

    def rollback_config(self, version_number: int) -> ResolvedConfiguration:
        """Rollback configuration to a previously recorded valid history version."""
        if not self._store:
            raise RuntimeError("Cannot rollback configuration: no persistent store configured.")

        res = self._store.rollback_to_version(version_number)
        layer = ConfigurationLayer(res["reverted_layer"])
        self._resolver.set_layer_config(layer, res["config"])
        logger.info("Rolled back layer '%s' to version %d", layer.value, version_number)
        return self.get_effective_config()

    # =========================================================================
    # CUSTOM MODES INTERFACE
    # =========================================================================

    def create_mode(
        self,
        name: str,
        description: str = "",
        autonomy_profile: Optional[AutonomyProfile] = None,
        security_policy: Optional[SecurityPolicy] = None,
        execution_profile: Optional[ExecutionProfile] = None,
        personality_config: Optional[PersonalityConfig] = None,
        is_default: bool = False,
    ) -> CustomMode:
        """Create a new reusable custom mode."""
        return self._modes.create_mode(
            name=name,
            description=description,
            autonomy_profile=autonomy_profile,
            security_policy=security_policy,
            execution_profile=execution_profile,
            personality_config=personality_config,
            is_default=is_default,
        )

    def get_mode(self, mode_id_or_name: str) -> Optional[CustomMode]:
        return self._modes.get_mode(mode_id_or_name)

    def list_modes(self) -> List[CustomMode]:
        return self._modes.list_modes()

    def update_mode(self, mode_id_or_name: str, updates: Dict[str, Any]) -> CustomMode:
        return self._modes.update_mode(mode_id_or_name, updates)

    def delete_mode(self, mode_id_or_name: str) -> bool:
        return self._modes.delete_mode(mode_id_or_name)

    def duplicate_mode(self, source_id_or_name: str, new_name: str) -> CustomMode:
        return self._modes.duplicate_mode(source_id_or_name, new_name)

    def activate_mode(self, mode_id_or_name: str) -> ResolvedConfiguration:
        """Activate a mode and apply its settings to the global configuration layer."""
        mode = self._modes.activate_mode(mode_id_or_name)

        # Apply mode components to the global layer
        self._resolver.set_layer_config(
            ConfigurationLayer.GLOBAL,
            {
                "active_mode": mode.name,
                "autonomy_profile": mode.autonomy_profile.model_dump(),
                "security_policy": mode.security_policy.model_dump(),
                "execution_profile": mode.execution_profile.model_dump(),
                "personality_config": mode.personality_config.model_dump(),
            },
        )
        return self.get_effective_config()

    def set_default_mode(self, mode_id_or_name: str) -> CustomMode:
        return self._modes.set_default_mode(mode_id_or_name)

    def apply_mode_to_agent(self, mode_id_or_name: str, agent_id: str) -> ResolvedConfiguration:
        """Apply a mode to the AGENT hierarchy layer."""
        mode = self._modes.get_mode(mode_id_or_name)
        if not mode:
            raise KeyError(f"Mode '{mode_id_or_name}' not found.")
        self._resolver.set_layer_config(
            ConfigurationLayer.AGENT,
            {
                "active_mode": f"{mode.name} (Agent:{agent_id})",
                "autonomy_profile": mode.autonomy_profile.model_dump(),
                "security_policy": mode.security_policy.model_dump(),
                "execution_profile": mode.execution_profile.model_dump(),
                "personality_config": mode.personality_config.model_dump(),
            },
        )
        return self.get_effective_config()

    def apply_mode_to_automation(self, mode_id_or_name: str, automation_id: str) -> ResolvedConfiguration:
        """Apply a mode to the AUTOMATION hierarchy layer."""
        mode = self._modes.get_mode(mode_id_or_name)
        if not mode:
            raise KeyError(f"Mode '{mode_id_or_name}' not found.")
        self._resolver.set_layer_config(
            ConfigurationLayer.AUTOMATION,
            {
                "active_mode": f"{mode.name} (Automation:{automation_id})",
                "autonomy_profile": mode.autonomy_profile.model_dump(),
                "security_policy": mode.security_policy.model_dump(),
                "execution_profile": mode.execution_profile.model_dump(),
                "personality_config": mode.personality_config.model_dump(),
            },
        )
        return self.get_effective_config()

    # =========================================================================
    # POLICY EVALUATION & EXPLANATION
    # =========================================================================

    def evaluate_policy(self, request: PolicyEvaluationRequest) -> PolicyEvaluationDecision:
        """Evaluate an action request against effective configuration and TrustCore."""
        return self._policy_engine.evaluate_action(request)

    def explain_policy_decision(self, request: PolicyEvaluationRequest) -> str:
        """Generate human-readable explanation of a policy evaluation decision."""
        return self._policy_engine.explain_decision(request)
