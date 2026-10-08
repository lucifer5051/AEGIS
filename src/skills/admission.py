"""SAGE Skills Subsystem -- Deterministic Resource & Concurrency Admission.

Integrates skill resource requirements with SAGE Model Resource Policy:
- Maps SkillManifest resource declarations to SkillResourceDescriptor.
- Evaluates live RAM headroom, VRAM headroom, and concurrency ceilings.
- Deterministic decision: PROCEED, REJECT, QUEUE, or DEFER.
- Skills never directly allocate or manage hardware resources.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.model_policy.concurrency_manager import SkillConcurrencyManager
from src.model_policy.models import (
    ConcurrencyMode,
    SkillExecutionLease,
    SkillResourceDescriptor,
)
from src.skills.models import SkillManifest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdmissionDecision:
    """Outcome of skill resource admission evaluation."""

    admitted: bool
    action: str  # "PROCEED", "REJECT", "QUEUE", "DEFER"
    reason: str
    mode: Optional[ConcurrencyMode] = None
    lease: Optional[SkillExecutionLease] = None


class SkillAdmissionController:
    """Deterministic admission gate for skill execution."""

    def __init__(
        self,
        concurrency_manager: Optional[SkillConcurrencyManager] = None,
    ) -> None:
        self._concurrency_manager = concurrency_manager

    def to_resource_descriptor(self, manifest: SkillManifest) -> SkillResourceDescriptor:
        """Convert a SkillManifest's resource requirements into a SkillResourceDescriptor."""
        res = manifest.resource_requirements
        return SkillResourceDescriptor(
            skill_id=manifest.skill_id,
            required_model_id=res.required_model_id,
            declared_marginal_ram_bytes=res.declared_marginal_ram_bytes,
            declared_marginal_vram_bytes=res.declared_marginal_vram_bytes,
            marginal_cpu_cores=res.marginal_cpu_cores,
            timeout_ms=min(30000, max(50, res.timeout_ms)),
            priority=res.priority,
        )

    def evaluate_admission(
        self,
        manifest: SkillManifest,
        acquire_lease: bool = True,
    ) -> AdmissionDecision:
        """Evaluate resource admission for the skill.

        Returns:
            AdmissionDecision with action (PROCEED, REJECT, QUEUE, DEFER).
        """
        descriptor = self.to_resource_descriptor(manifest)

        if self._concurrency_manager is None:
            # Standalone / unmanaged mode (e.g. lightweight offline tests)
            return AdmissionDecision(
                admitted=True,
                action="PROCEED",
                reason="No concurrency manager attached; unmanaged admission permitted.",
            )

        evaluation = self._concurrency_manager.evaluate_skill_admission(descriptor)

        if not evaluation.permitted:
            action = "REJECT"
            if evaluation.mode == ConcurrencyMode.CONSTRAINED:
                action = "DEFER"
            elif evaluation.mode == ConcurrencyMode.CRITICAL:
                action = "QUEUE"

            logger.warning(
                "Admission denied for skill '%s': %s (action=%s, mode=%s)",
                manifest.skill_id,
                evaluation.reason,
                action,
                evaluation.mode.value,
            )
            return AdmissionDecision(
                admitted=False,
                action=action,
                reason=evaluation.reason,
                mode=evaluation.mode,
            )

        lease: Optional[SkillExecutionLease] = None
        if acquire_lease:
            lease = self._concurrency_manager.acquire_skill_lease(descriptor)
            if lease is None:
                return AdmissionDecision(
                    admitted=False,
                    action="DEFER",
                    reason="Failed to acquire lease despite evaluation permit.",
                    mode=evaluation.mode,
                )

        return AdmissionDecision(
            admitted=True,
            action="PROCEED",
            reason=evaluation.reason,
            mode=evaluation.mode,
            lease=lease,
        )

    def release_lease(self, lease: SkillExecutionLease) -> None:
        """Release an active skill execution lease."""
        if self._concurrency_manager is not None:
            self._concurrency_manager.release_skill_lease(lease)
