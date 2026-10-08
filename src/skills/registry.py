"""SAGE Skills Subsystem -- Deterministic Skill Registry.

Maintains registered skills, validates manifests upon registration,
tracks lifecycle transitions, and performs deterministic search and matching.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from src.skills.errors import (
    SkillDuplicateError,
    SkillLifecycleError,
    SkillNotFoundError,
)
from src.skills.models import (
    RegisteredSkill,
    SkillLifecycleState,
    SkillManifest,
)
from src.skills.validation import validate_skill_manifest

logger = logging.getLogger(__name__)

# Deterministic Lifecycle State Machine Transitions
VALID_LIFECYCLE_TRANSITIONS: Dict[SkillLifecycleState, FrozenSet[SkillLifecycleState]] = {
    SkillLifecycleState.DISCOVERED: frozenset({SkillLifecycleState.VALIDATING}),
    SkillLifecycleState.VALIDATING: frozenset(
        {SkillLifecycleState.VERIFIED, SkillLifecycleState.FAILED}
    ),
    SkillLifecycleState.VERIFIED: frozenset(
        {SkillLifecycleState.REGISTERED, SkillLifecycleState.FAILED}
    ),
    SkillLifecycleState.REGISTERED: frozenset(
        {SkillLifecycleState.AVAILABLE, SkillLifecycleState.FAILED}
    ),
    SkillLifecycleState.AVAILABLE: frozenset(
        {SkillLifecycleState.SELECTED, SkillLifecycleState.REGISTERED}
    ),
    SkillLifecycleState.SELECTED: frozenset(
        {SkillLifecycleState.ADMITTED, SkillLifecycleState.FAILED}
    ),
    SkillLifecycleState.ADMITTED: frozenset(
        {SkillLifecycleState.EXECUTING, SkillLifecycleState.FAILED}
    ),
    SkillLifecycleState.EXECUTING: frozenset(
        {SkillLifecycleState.VERIFYING, SkillLifecycleState.FAILED}
    ),
    SkillLifecycleState.VERIFYING: frozenset(
        {SkillLifecycleState.SUCCEEDED, SkillLifecycleState.FAILED}
    ),
    SkillLifecycleState.SUCCEEDED: frozenset({SkillLifecycleState.AVAILABLE}),
    SkillLifecycleState.FAILED: frozenset(
        {SkillLifecycleState.RECOVERY, SkillLifecycleState.AVAILABLE}
    ),
    SkillLifecycleState.RECOVERY: frozenset(
        {
            SkillLifecycleState.EXECUTING,
            SkillLifecycleState.FAILED,
            SkillLifecycleState.AVAILABLE,
        }
    ),
}


class SkillRegistry:
    """Deterministic, fail-closed registry of SAGE skills."""

    def __init__(self, known_tools: Optional[Sequence[str]] = None) -> None:
        self._skills: Dict[str, RegisteredSkill] = {}
        self._executors: Dict[str, Any] = {}
        self._known_tools: Optional[Sequence[str]] = known_tools

    def register_skill(
        self,
        manifest: SkillManifest,
        executor: Optional[Any] = None,
    ) -> RegisteredSkill:
        """Register a skill manifest deterministically.

        Steps:
        1. Check for duplicate ID (fail-closed).
        2. Validate manifest (DISCOVERED -> VALIDATING -> VERIFIED).
        3. Transition to REGISTERED -> AVAILABLE.
        4. Store immutable record and optional executor.

        Args:
            manifest: Declarative SkillManifest.
            executor: Optional callable or SkillExecutor instance.

        Returns:
            RegisteredSkill record.

        Raises:
            SkillDuplicateError: If skill_id is already registered.
            SkillManifestValidationError: If manifest validation fails.
        """
        skill_id = manifest.skill_id

        if skill_id in self._skills:
            raise SkillDuplicateError(
                f"Skill '{skill_id}' is already registered. Duplicate registrations rejected."
            )

        # 1. Validation pipeline
        validate_skill_manifest(manifest, known_tools=self._known_tools)

        now = datetime.now(timezone.utc).isoformat()

        # Update manifest to AVAILABLE for active operational readiness
        admitted_manifest = manifest.model_copy(
            update={"lifecycle_state": SkillLifecycleState.AVAILABLE}
        )

        reg = RegisteredSkill(
            manifest=admitted_manifest,
            registered_at_utc=now,
            current_state=SkillLifecycleState.AVAILABLE,
        )

        self._skills[skill_id] = reg
        if executor is not None:
            self._executors[skill_id] = executor

        logger.info("Successfully registered skill '%s' (v%s)", skill_id, manifest.version)
        return reg

    def unregister_skill(self, skill_id: str) -> None:
        """Remove a skill from the registry.

        Raises:
            SkillNotFoundError: If skill is not registered.
        """
        if skill_id not in self._skills:
            raise SkillNotFoundError(f"Cannot unregister: skill '{skill_id}' not found.")

        del self._skills[skill_id]
        self._executors.pop(skill_id, None)
        logger.info("Unregistered skill '%s'", skill_id)

    def get_skill(self, skill_id: str) -> RegisteredSkill:
        """Fetch a registered skill by ID.

        Raises:
            SkillNotFoundError: If skill is not registered.
        """
        if skill_id not in self._skills:
            raise SkillNotFoundError(f"Skill '{skill_id}' is not registered.")
        return self._skills[skill_id]

    def find_skill(self, skill_id: str) -> Optional[RegisteredSkill]:
        """Fetch a registered skill by ID, returning None if absent."""
        return self._skills.get(skill_id)

    def get_executor(self, skill_id: str) -> Optional[Any]:
        """Fetch the registered executor for a skill, if any."""
        return self._executors.get(skill_id)

    def list_skills(
        self,
        lifecycle_state: Optional[SkillLifecycleState] = None,
    ) -> List[RegisteredSkill]:
        """List registered skills, optionally filtered by lifecycle state."""
        skills = list(self._skills.values())
        if lifecycle_state is not None:
            skills = [s for s in skills if s.current_state == lifecycle_state]
        return sorted(skills, key=lambda s: s.manifest.skill_id)

    def search_skills(
        self,
        intent: Optional[str] = None,
        capability: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> List[RegisteredSkill]:
        """Search skills deterministically matching intent, capability, or tags."""
        results: List[RegisteredSkill] = []

        target_tags = {t.lower() for t in tags} if tags else None
        target_intent = intent.lower() if intent else None

        for reg in self._skills.values():
            m = reg.manifest

            # Capability filter
            if capability and capability not in m.required_capabilities:
                continue

            # Tag filter
            if target_tags:
                manifest_tags = {t.lower() for t in m.intent_tags}
                if not target_tags.issubset(manifest_tags):
                    continue

            # Intent filter (deterministic match against tags and description)
            if target_intent:
                manifest_tag_list = [t.lower() for t in m.intent_tags]
                tag_match = any(target_intent in t or t in target_intent for t in manifest_tag_list)
                desc_match = (
                    target_intent in m.description.lower() or target_intent in m.name.lower()
                )
                if not (tag_match or desc_match):
                    continue

            results.append(reg)

        return sorted(results, key=lambda s: s.manifest.skill_id)

    def transition_lifecycle(
        self,
        skill_id: str,
        target_state: SkillLifecycleState,
    ) -> RegisteredSkill:
        """Transition a registered skill to a new lifecycle state.

        Validates the transition deterministically against VALID_LIFECYCLE_TRANSITIONS.

        Raises:
            SkillNotFoundError: If skill is not registered.
            SkillLifecycleError: If transition is forbidden.
        """
        reg = self.get_skill(skill_id)
        current = reg.current_state

        if current == target_state:
            return reg

        allowed = VALID_LIFECYCLE_TRANSITIONS.get(current, frozenset())
        if target_state not in allowed:
            raise SkillLifecycleError(
                f"Illegal lifecycle transition for skill '{skill_id}': "
                f"'{current.value}' -> '{target_state.value}'. "
                f"Allowed target states: {[s.value for s in allowed]}"
            )

        updated_manifest = reg.manifest.model_copy(update={"lifecycle_state": target_state})
        updated_reg = reg.model_copy(
            update={"current_state": target_state, "manifest": updated_manifest}
        )
        self._skills[skill_id] = updated_reg
        logger.debug(
            "Skill '%s' transitioned: %s -> %s", skill_id, current.value, target_state.value
        )
        return updated_reg
