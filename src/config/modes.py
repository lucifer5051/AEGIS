"""SAGE Custom Mode Subsystem -- Reusable Multi-Dimensional Profiles.

Canonical Reference:
    PROMPT 069: SAGE Configuration & Policy System

Provides lifecycle management for reusable custom modes:
- Create, Read, Update, Delete, Duplicate.
- Activation and Default configuration.
- Agent and Automation mode application.
- Built-in templates: Coding Mode, Research Mode, Study Mode, Personal Assistant Mode, Private Mode, Automation Mode.
- Security constraints: modes cannot override immutable TrustCore rules.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.config.models import AutonomyTier, NetworkMode
from src.config.policy_models import (
    AutonomyMode,
    AutonomyProfile,
    CustomMode,
    ExecutionProfile,
    ExecutionProfileType,
    FilesystemPolicy,
    NetworkPolicy,
    PersonalityConfig,
    SecurityPolicy,
    ShellPolicy,
    ToolPolicy,
)
from src.config.policy_store import PolicyConfigStore
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class CustomModeManager:
    """Manages reusable multi-dimensional custom modes."""

    def __init__(self, store: Optional[PolicyConfigStore] = None) -> None:
        self._store = store
        self._memory_modes: Dict[str, CustomMode] = {}
        self._active_mode_name: Optional[str] = None
        self._register_builtin_templates()

    def _register_builtin_templates(self) -> None:
        """Seed canonical built-in custom mode templates."""
        templates = [
            CustomMode(
                mode_id="builtin-coding-mode",
                name="Coding Mode",
                description="Optimized for software engineering: supervised autonomy, code tools, balanced execution.",
                autonomy_profile=AutonomyProfile.builtin_supervised(),
                security_policy=SecurityPolicy(
                    name="Coding Security Policy",
                    filesystem=FilesystemPolicy(allowed_paths=["."]),
                    shell=ShellPolicy(enabled=True, allowed_commands=["pytest", "git", "python"]),
                    tools=ToolPolicy(allowed_tools=["read_file", "write_file", "edit_file", "run_command"]),
                ),
                execution_profile=ExecutionProfile.builtin_balanced(),
                personality_config=PersonalityConfig(
                    agent_name="SAGE",
                    tone="concise, senior software engineer",
                    verbosity="low",
                ),
            ),
            CustomMode(
                mode_id="builtin-research-mode",
                name="Research Mode",
                description="Deep analysis and investigation: assisted autonomy, comprehensive context, thorough verification.",
                autonomy_profile=AutonomyProfile.builtin_assisted(),
                security_policy=SecurityPolicy(
                    name="Research Security Policy",
                    network=NetworkPolicy(mode=NetworkMode.BALANCED),
                    tools=ToolPolicy(allowed_tools=["read_file", "search_web", "query_database"]),
                ),
                execution_profile=ExecutionProfile.builtin_deep(),
                personality_config=PersonalityConfig(
                    agent_name="SAGE",
                    tone="rigorous, comprehensive, academic",
                    verbosity="high",
                ),
            ),
            CustomMode(
                mode_id="builtin-study-mode",
                name="Study Mode",
                description="Interactive learning: assisted autonomy, balanced execution, educational explanations.",
                autonomy_profile=AutonomyProfile.builtin_assisted(),
                security_policy=SecurityPolicy(name="Study Security Policy"),
                execution_profile=ExecutionProfile.builtin_balanced(),
                personality_config=PersonalityConfig(
                    agent_name="SAGE",
                    tone="patient, didactic, clear",
                    verbosity="medium",
                ),
            ),
            CustomMode(
                mode_id="builtin-assistant-mode",
                name="Personal Assistant Mode",
                description="General personal companion: fast execution, friendly personality.",
                autonomy_profile=AutonomyProfile.builtin_assisted(),
                security_policy=SecurityPolicy(name="Assistant Security Policy"),
                execution_profile=ExecutionProfile.builtin_fast(),
                personality_config=PersonalityConfig(
                    agent_name="Friday",
                    tone="warm, proactive, concise",
                    verbosity="medium",
                ),
            ),
            CustomMode(
                mode_id="builtin-private-mode",
                name="Private Mode",
                description="Maximum security and confidentiality: manual approval, local-only network, shell disabled.",
                autonomy_profile=AutonomyProfile.builtin_manual(),
                security_policy=SecurityPolicy(
                    name="Strict Private Policy",
                    network=NetworkPolicy(mode=NetworkMode.LOCAL_ONLY),
                    shell=ShellPolicy(enabled=False),
                ),
                execution_profile=ExecutionProfile.builtin_balanced(),
                personality_config=PersonalityConfig(
                    agent_name="SAGE",
                    tone="guarded, formal",
                    verbosity="low",
                ),
            ),
            CustomMode(
                mode_id="builtin-automation-mode",
                name="Automation Mode",
                description="Unattended scheduled tasks: supervised autonomy with strict bounded step limits.",
                autonomy_profile=AutonomyProfile(
                    mode=AutonomyMode.SUPERVISED,
                    name="Automation Autonomy",
                    tier=AutonomyTier.TIER_2_SUPERVISED,
                    checkpoint_required_after_steps=5,
                    allowed_autonomous_risks=["LOW"],
                ),
                security_policy=SecurityPolicy(name="Automation Security Policy"),
                execution_profile=ExecutionProfile(
                    profile_type=ExecutionProfileType.FAST,
                    name="Automation Fast Execution",
                    context_token_budget=2048,
                    max_total_steps=10,
                ),
                personality_config=PersonalityConfig(agent_name="SAGE", verbosity="low"),
            ),
        ]

        for t in templates:
            self._memory_modes[t.name.lower()] = t
            self._memory_modes[t.mode_id] = t
            if self._store:
                existing = self._store.get_mode(t.name)
                if not existing:
                    self._store.save_mode(t)

    # =========================================================================
    # LIFECYCLE OPERATIONS
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
        """Create and register a new custom mode."""
        _check_no_secrets(name)
        _check_no_secrets(description)

        mode = CustomMode(
            mode_id=str(uuid4()),
            name=name.strip(),
            description=description.strip(),
            autonomy_profile=autonomy_profile or AutonomyProfile.builtin_assisted(),
            security_policy=security_policy or SecurityPolicy(),
            execution_profile=execution_profile or ExecutionProfile.builtin_balanced(),
            personality_config=personality_config or PersonalityConfig(),
            is_default=is_default,
            created_at=_utc_now_iso(),
            updated_at=_utc_now_iso(),
        )

        self._memory_modes[mode.name.lower()] = mode
        self._memory_modes[mode.mode_id] = mode
        if self._store:
            self._store.save_mode(mode)

        logger.info("Created custom mode '%s' (id=%s)", mode.name, mode.mode_id)
        return mode

    def get_mode(self, mode_id_or_name: str) -> Optional[CustomMode]:
        """Lookup a mode by ID or case-insensitive name."""
        key = mode_id_or_name.lower()
        if key in self._memory_modes:
            return self._memory_modes[key]
        if self._store:
            stored = self._store.get_mode(mode_id_or_name)
            if stored:
                self._memory_modes[stored.name.lower()] = stored
                self._memory_modes[stored.mode_id] = stored
                return stored
        return None

    def list_modes(self) -> List[CustomMode]:
        """List all unique registered modes."""
        if self._store:
            db_modes = self._store.list_modes()
            for m in db_modes:
                self._memory_modes[m.name.lower()] = m
                self._memory_modes[m.mode_id] = m
        # De-duplicate by mode_id
        seen = set()
        result = []
        for m in self._memory_modes.values():
            if m.mode_id not in seen:
                seen.add(m.mode_id)
                result.append(m)
        result.sort(key=lambda m: (not m.is_default, m.name))
        return result

    def update_mode(
        self,
        mode_id_or_name: str,
        updates: Dict[str, Any],
    ) -> CustomMode:
        """Update an existing custom mode."""
        existing = self.get_mode(mode_id_or_name)
        if not existing:
            raise KeyError(f"Custom mode '{mode_id_or_name}' not found.")

        _check_no_secrets(str(updates))

        updated_dict = existing.model_dump()
        for k, v in updates.items():
            if k in updated_dict and k not in ("mode_id", "created_at"):
                updated_dict[k] = v
        updated_dict["updated_at"] = _utc_now_iso()

        updated_mode = CustomMode.model_validate(updated_dict)
        self._memory_modes[updated_mode.name.lower()] = updated_mode
        self._memory_modes[updated_mode.mode_id] = updated_mode
        if self._store:
            self._store.save_mode(updated_mode)

        logger.info("Updated custom mode '%s'", updated_mode.name)
        return updated_mode

    def delete_mode(self, mode_id_or_name: str) -> bool:
        """Delete a custom mode by ID or name (built-ins cannot be deleted)."""
        mode = self.get_mode(mode_id_or_name)
        if not mode:
            return False

        if mode.mode_id.startswith("builtin-"):
            raise ValueError(f"Cannot delete built-in mode '{mode.name}'.")

        self._memory_modes.pop(mode.name.lower(), None)
        self._memory_modes.pop(mode.mode_id, None)

        if self._store:
            return self._store.delete_mode(mode.mode_id)
        return True

    def duplicate_mode(self, source_id_or_name: str, new_name: str) -> CustomMode:
        """Duplicate an existing mode under a new unique name."""
        source = self.get_mode(source_id_or_name)
        if not source:
            raise KeyError(f"Source mode '{source_id_or_name}' not found.")

        _check_no_secrets(new_name)

        duplicated = CustomMode(
            mode_id=str(uuid4()),
            name=new_name.strip(),
            description=f"Duplicate of {source.name}. {source.description}",
            autonomy_profile=source.autonomy_profile.model_copy(deep=True),
            security_policy=source.security_policy.model_copy(deep=True),
            execution_profile=source.execution_profile.model_copy(deep=True),
            personality_config=source.personality_config.model_copy(deep=True),
            is_default=False,
            created_at=_utc_now_iso(),
            updated_at=_utc_now_iso(),
        )
        self._memory_modes[duplicated.name.lower()] = duplicated
        self._memory_modes[duplicated.mode_id] = duplicated
        if self._store:
            self._store.save_mode(duplicated)

        return duplicated

    def activate_mode(self, mode_id_or_name: str) -> CustomMode:
        """Activate a mode for the current session."""
        mode = self.get_mode(mode_id_or_name)
        if not mode:
            raise KeyError(f"Mode '{mode_id_or_name}' not found.")
        self._active_mode_name = mode.name
        logger.info("Activated mode: %s", mode.name)
        return mode

    def get_active_mode(self) -> Optional[CustomMode]:
        """Get the currently activated custom mode, if any."""
        if self._active_mode_name:
            return self.get_mode(self._active_mode_name)
        if self._store:
            return self._store.get_default_mode()
        return None

    def set_default_mode(self, mode_id_or_name: str) -> CustomMode:
        """Mark a mode as the default system-wide mode."""
        mode = self.get_mode(mode_id_or_name)
        if not mode:
            raise KeyError(f"Mode '{mode_id_or_name}' not found.")

        # Update in-memory
        for m in self._memory_modes.values():
            if m.mode_id == mode.mode_id:
                object.__setattr__(m, "is_default", True)
            else:
                object.__setattr__(m, "is_default", False)

        if self._store:
            self._store.save_mode(mode.model_copy(update={"is_default": True}))

        return mode
