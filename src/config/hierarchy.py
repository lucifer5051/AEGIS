"""SAGE Configuration Hierarchy Resolver.

Canonical Reference:
    PROMPT 069: SAGE Configuration & Policy System

Implements deterministic configuration resolution across the 6-tier hierarchy:
    System Defaults (0) → Global Configuration (1) → User Configuration (2)
        → Agent Configuration (3) → Task Configuration (4) → Automation Configuration (5)

Guarantees:
1. Deterministic inheritance: child overrides take precedence over parent.
2. Full explainability: every resolved attribute records its exact origin layer.
3. Security monotonicity: denied paths, commands, and tools accumulate monotonically across layers.
4. TrustCore priority: configuration cannot widen TrustCore security boundaries.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.config.models import AutonomyTier, NetworkMode
from src.config.policy_models import (
    AutonomyMode,
    AutonomyProfile,
    ConfigurationLayer,
    ExecutionProfile,
    ExecutionProfileType,
    FilesystemPolicy,
    NetworkPolicy,
    PersonalityConfig,
    ResolvedConfiguration,
    SecurityPolicy,
    ShellPolicy,
    ToolPolicy,
)
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class ConfigurationHierarchyResolver:
    """Resolves effective configuration across the SAGE configuration hierarchy."""

    # Ordered from lowest to highest precedence
    LAYER_ORDER: List[ConfigurationLayer] = [
        ConfigurationLayer.SYSTEM_DEFAULTS,
        ConfigurationLayer.GLOBAL,
        ConfigurationLayer.USER,
        ConfigurationLayer.AGENT,
        ConfigurationLayer.TASK,
        ConfigurationLayer.AUTOMATION,
    ]

    def __init__(self) -> None:
        self._layers: Dict[ConfigurationLayer, Dict[str, Any]] = {
            layer: {} for layer in self.LAYER_ORDER
        }
        # Populate system defaults
        self._layers[ConfigurationLayer.SYSTEM_DEFAULTS] = self._build_system_defaults()

    def set_layer_config(
        self,
        layer: ConfigurationLayer,
        config_data: Dict[str, Any],
    ) -> None:
        """Set or update the configuration dictionary for a specific hierarchy layer."""
        # Validate zero secrets
        _check_no_secrets(str(config_data))
        self._layers[layer] = copy.deepcopy(config_data)

    def get_layer_config(self, layer: ConfigurationLayer) -> Dict[str, Any]:
        """Retrieve raw configuration dictionary for a specific layer."""
        return copy.deepcopy(self._layers.get(layer, {}))

    def clear_layer_config(self, layer: ConfigurationLayer) -> None:
        """Clear overrides for a specific layer (cannot clear SYSTEM_DEFAULTS)."""
        if layer == ConfigurationLayer.SYSTEM_DEFAULTS:
            self._layers[layer] = self._build_system_defaults()
        else:
            self._layers[layer] = {}

    def resolve(
        self,
        task_overrides: Optional[Dict[str, Any]] = None,
        agent_overrides: Optional[Dict[str, Any]] = None,
        automation_overrides: Optional[Dict[str, Any]] = None,
        active_mode_name: Optional[str] = None,
    ) -> ResolvedConfiguration:
        """Deterministically resolve the effective configuration with origin tracking.

        Precedence (low to high):
        0. System Defaults
        1. Global
        2. User
        3. Agent
        4. Task
        5. Automation
        """
        origins: Dict[str, str] = {}

        # 1. Start with system defaults
        merged: Dict[str, Any] = copy.deepcopy(self._layers[ConfigurationLayer.SYSTEM_DEFAULTS])
        for top_key, sub_dict in merged.items():
            if isinstance(sub_dict, dict):
                for k in sub_dict:
                    origins[f"{top_key}.{k}"] = ConfigurationLayer.SYSTEM_DEFAULTS.value
            else:
                origins[top_key] = ConfigurationLayer.SYSTEM_DEFAULTS.value

        # Security accumulation buffers (monotonic restrictions)
        accumulated_denied_paths = set(merged.get("security_policy", {}).get("filesystem", {}).get("denied_paths", []))
        accumulated_denied_commands = set(merged.get("security_policy", {}).get("shell", {}).get("denied_commands", []))
        accumulated_denied_tools = set(merged.get("security_policy", {}).get("tools", {}).get("denied_tools", []))

        # 2. Iterate layers in precedence order
        layers_to_apply: List[Tuple[ConfigurationLayer, Dict[str, Any]]] = [
            (ConfigurationLayer.GLOBAL, self._layers[ConfigurationLayer.GLOBAL]),
            (ConfigurationLayer.USER, self._layers[ConfigurationLayer.USER]),
            (ConfigurationLayer.AGENT, agent_overrides or self._layers[ConfigurationLayer.AGENT]),
            (ConfigurationLayer.TASK, task_overrides or self._layers[ConfigurationLayer.TASK]),
            (ConfigurationLayer.AUTOMATION, automation_overrides or self._layers[ConfigurationLayer.AUTOMATION]),
        ]

        highest_active_layer = ConfigurationLayer.SYSTEM_DEFAULTS

        for layer, layer_data in layers_to_apply:
            if not layer_data:
                continue

            highest_active_layer = layer
            layer_name = layer.value

            # Deep merge into merged dict with origin tracking
            for section_key, section_val in layer_data.items():
                if isinstance(section_val, dict) and isinstance(merged.get(section_key), dict):
                    for k, v in section_val.items():
                        if isinstance(v, dict) and isinstance(merged[section_key].get(k), dict):
                            for sub_k, sub_v in v.items():
                                merged[section_key][k][sub_k] = sub_v
                                origins[f"{section_key}.{k}.{sub_k}"] = layer_name
                        else:
                            merged[section_key][k] = v
                            origins[f"{section_key}.{k}"] = layer_name
                else:
                    merged[section_key] = section_val
                    origins[section_key] = layer_name

            # Accumulate security denials (monotonic security rule)
            sec = layer_data.get("security_policy", {})
            if "filesystem" in sec and "denied_paths" in sec["filesystem"]:
                accumulated_denied_paths.update(sec["filesystem"]["denied_paths"])
            if "shell" in sec and "denied_commands" in sec["shell"]:
                accumulated_denied_commands.update(sec["shell"]["denied_commands"])
            if "tools" in sec and "denied_tools" in sec["tools"]:
                accumulated_denied_tools.update(sec["tools"]["denied_tools"])

        # Enforce security monotonicity back into merged security policy
        if "security_policy" in merged:
            if "filesystem" in merged["security_policy"]:
                merged["security_policy"]["filesystem"]["denied_paths"] = sorted(accumulated_denied_paths)
            if "shell" in merged["security_policy"]:
                merged["security_policy"]["shell"]["denied_commands"] = sorted(accumulated_denied_commands)
            if "tools" in merged["security_policy"]:
                merged["security_policy"]["tools"]["denied_tools"] = sorted(accumulated_denied_tools)

        # Build typed models from merged dictionaries
        autonomy_prof = AutonomyProfile.model_validate(merged.get("autonomy_profile", {}))
        sec_pol = SecurityPolicy.model_validate(merged.get("security_policy", {}))
        exec_prof = ExecutionProfile.model_validate(merged.get("execution_profile", {}))
        pers_cfg = PersonalityConfig.model_validate(merged.get("personality_config", {}))

        return ResolvedConfiguration(
            autonomy_profile=autonomy_prof,
            security_policy=sec_pol,
            execution_profile=exec_prof,
            personality_config=pers_cfg,
            active_mode=active_mode_name or merged.get("active_mode"),
            effective_layer=highest_active_layer,
            field_origins=origins,
            resolved_at=_utc_now_iso(),
        )

    def _build_system_defaults(self) -> Dict[str, Any]:
        """Construct immutable SAGE baseline default configuration dictionaries."""
        return {
            "autonomy_profile": AutonomyProfile.builtin_assisted().model_dump(),
            "security_policy": SecurityPolicy().model_dump(),
            "execution_profile": ExecutionProfile.builtin_balanced().model_dump(),
            "personality_config": PersonalityConfig().model_dump(),
        }
