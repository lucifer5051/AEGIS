"""AEGIS Skill Concurrency Manager - Dynamic Admission & Shared Residency Control.

Enforces the AEGIS architectural target of 2–3 concurrent skills.
Calculates admission decisions dynamically using:
1. Live hardware measurements (LiveHardwareBenchmark).
2. Empirically measured resident and marginal memory (ModelHardwareQualificationProfile).
3. Configurable / qualification-derived system safety reserves (never hardcoded).
4. Shared model residency reference counting (ModelResidencyManager).
5. Heterogeneous workloads (lightweight skills coexist alongside heavyweight models).
"""

import uuid
from typing import Dict, List, Optional

from src.model_policy.live_benchmark import LiveHardwareBenchmark
from src.model_policy.models import (
    ConcurrencyEvaluation,
    ConcurrencyMode,
    ModelHardwareQualificationProfile,
    SkillExecutionLease,
    SkillResourceDescriptor,
)
from src.model_policy.residency_manager import ModelResidencyManager


class SkillConcurrencyManager:
    """Dynamic admission controller for concurrent skill execution."""

    def __init__(
        self,
        residency_manager: ModelResidencyManager,
        benchmark: Optional[LiveHardwareBenchmark] = None,
        system_ram_reserve_bytes: int = 1536 * 1024 * 1024,  # Configurable 1.5 GB default
        system_vram_reserve_bytes: int = 512 * 1024 * 1024,  # Configurable 512 MB default
        target_concurrent_skills: int = 3,  # Configurable target ceiling
    ) -> None:
        self.residency_manager = residency_manager
        self.benchmark = benchmark or LiveHardwareBenchmark()
        self.system_ram_reserve_bytes = system_ram_reserve_bytes
        self.system_vram_reserve_bytes = system_vram_reserve_bytes
        self.target_concurrent_skills = target_concurrent_skills

        # Map of model_id -> qualification profile
        self._qualification_profiles: Dict[str, ModelHardwareQualificationProfile] = {}
        # Map of lease_id -> active SkillExecutionLease
        self._active_leases: Dict[str, SkillExecutionLease] = {}

    def register_qualification_profile(self, profile: ModelHardwareQualificationProfile) -> None:
        """Register an empirical hardware qualification profile for a model."""
        self._qualification_profiles[profile.model_id] = profile

    def update_safety_reserves(
        self,
        system_ram_reserve_bytes: Optional[int] = None,
        system_vram_reserve_bytes: Optional[int] = None,
        target_concurrent_skills: Optional[int] = None,
    ) -> None:
        """Update configurable / qualification-derived safety reserves."""
        if system_ram_reserve_bytes is not None:
            self.system_ram_reserve_bytes = system_ram_reserve_bytes
        if system_vram_reserve_bytes is not None:
            self.system_vram_reserve_bytes = system_vram_reserve_bytes
        if target_concurrent_skills is not None:
            self.target_concurrent_skills = target_concurrent_skills

    def get_active_leases(self) -> List[SkillExecutionLease]:
        """List all currently active skill execution leases."""
        return list(self._active_leases.values())

    def get_active_skill_count(self) -> int:
        """Get the count of currently active skill execution leases."""
        return len(self._active_leases)

    def determine_concurrency_mode(
        self, avail_ram_bytes: int, free_vram_bytes: int
    ) -> ConcurrencyMode:
        """Determine concurrency mode based on live memory headroom."""
        if avail_ram_bytes < 2200 * 1024 * 1024 or free_vram_bytes < self.system_vram_reserve_bytes:
            return ConcurrencyMode.CRITICAL
        if avail_ram_bytes < 4500 * 1024 * 1024:
            return ConcurrencyMode.CONSTRAINED
        if avail_ram_bytes < 6000 * 1024 * 1024:
            return ConcurrencyMode.NORMAL
        return ConcurrencyMode.OPTIMAL

    def evaluate_skill_admission(
        self, descriptor: SkillResourceDescriptor
    ) -> ConcurrencyEvaluation:
        """Evaluate whether a skill execution lease can be granted safely."""
        avail_ram, _, free_vram, _ = self.benchmark.query_live_memory()
        mode = self.determine_concurrency_mode(avail_ram, free_vram)
        active_count = len(self._active_leases)

        # 1. Calculate unreserved system headroom
        ram_headroom = max(0, avail_ram - self.system_ram_reserve_bytes)
        vram_headroom = max(0, free_vram - self.system_vram_reserve_bytes)

        # 2. Case A: Lightweight non-model skill (e.g. regex, validator, heuristic)
        if descriptor.required_model_id is None:
            marginal_ram = descriptor.declared_marginal_ram_bytes
            if ram_headroom < marginal_ram:
                return ConcurrencyEvaluation(
                    permitted=False,
                    active_skill_count=active_count,
                    max_safe_skills=max(1, active_count),
                    mode=mode,
                    reason=f"Insufficient RAM headroom ({ram_headroom // (1024 * 1024)} MB free) "
                    f"for lightweight skill ({marginal_ram // (1024 * 1024)} MB needed).",
                    measured_free_ram_bytes=avail_ram,
                    measured_free_vram_bytes=free_vram,
                )
            if active_count >= self.target_concurrent_skills:
                return ConcurrencyEvaluation(
                    permitted=False,
                    active_skill_count=active_count,
                    max_safe_skills=self.target_concurrent_skills,
                    mode=mode,
                    reason=f"Active skills ({active_count}) reached target ceiling "
                    f"({self.target_concurrent_skills}).",
                    measured_free_ram_bytes=avail_ram,
                    measured_free_vram_bytes=free_vram,
                )
            return ConcurrencyEvaluation(
                permitted=True,
                active_skill_count=active_count,
                max_safe_skills=self.target_concurrent_skills,
                mode=mode,
                reason="Lightweight skill permitted within available headroom.",
                measured_free_ram_bytes=avail_ram,
                measured_free_vram_bytes=free_vram,
            )

        # 3. Case B: Heavy ML model skill (requires model residency)
        model_id = descriptor.required_model_id
        is_already_resident = self.residency_manager.is_resident(model_id)
        qual = self._qualification_profiles.get(model_id)

        # Retrieve qualification-derived metrics or fallbacks
        if qual is not None:
            res_ram = qual.measured_resident_ram_bytes
            res_vram = qual.measured_resident_vram_bytes
            marginal_ram = max(
                qual.measured_marginal_ram_bytes, descriptor.declared_marginal_ram_bytes
            )
            marginal_vram = max(
                qual.measured_marginal_vram_bytes, descriptor.declared_marginal_vram_bytes
            )
        else:
            # Conservative defaults if uncalibrated
            res_ram = 3200 * 1024 * 1024
            res_vram = 1600 * 1024 * 1024
            marginal_ram = descriptor.declared_marginal_ram_bytes
            marginal_vram = descriptor.declared_marginal_vram_bytes

        # If model is not loaded yet, base residency must fit into headroom
        needed_ram = (0 if is_already_resident else res_ram) + marginal_ram
        needed_vram = (0 if is_already_resident else res_vram) + marginal_vram

        if ram_headroom < needed_ram:
            res_mb = res_ram // (1024 * 1024)
            marg_mb = marginal_ram // (1024 * 1024)
            head_mb = ram_headroom // (1024 * 1024)
            live_mb = avail_ram // (1024 * 1024)
            resv_mb = self.system_ram_reserve_bytes // (1024 * 1024)
            return ConcurrencyEvaluation(
                permitted=False,
                active_skill_count=active_count,
                max_safe_skills=0 if not is_already_resident else active_count,
                mode=mode,
                reason=(
                    f"RAM memory gate: needed {needed_ram // (1024 * 1024)} MB "
                    f"(resident={res_mb} MB, marginal={marg_mb} MB), "
                    f"but available headroom is only {head_mb} MB "
                    f"(live free={live_mb} MB, reserve={resv_mb} MB)."
                ),
                measured_free_ram_bytes=avail_ram,
                measured_free_vram_bytes=free_vram,
            )

        if vram_headroom < needed_vram:
            return ConcurrencyEvaluation(
                permitted=False,
                active_skill_count=active_count,
                max_safe_skills=0 if not is_already_resident else active_count,
                mode=mode,
                reason=f"VRAM memory gate: needed {needed_vram // (1024 * 1024)} MB, "
                f"but free VRAM headroom is {vram_headroom // (1024 * 1024)} MB.",
                measured_free_ram_bytes=avail_ram,
                measured_free_vram_bytes=free_vram,
            )

        # Dynamic safe skill limit derivation: min(target_ceiling, 1 + remaining / marginal)
        remaining_ram_after_model = ram_headroom - (0 if is_already_resident else res_ram)
        capacity_skills = 1 + (remaining_ram_after_model // max(1, marginal_ram))
        max_safe_skills = min(self.target_concurrent_skills, int(capacity_skills))

        if active_count >= max_safe_skills:
            return ConcurrencyEvaluation(
                permitted=False,
                active_skill_count=active_count,
                max_safe_skills=max_safe_skills,
                mode=mode,
                reason=f"Active skills ({active_count}) reached qualification-derived safe limit "
                f"({max_safe_skills}).",
                measured_free_ram_bytes=avail_ram,
                measured_free_vram_bytes=free_vram,
            )

        m_ram_mb = marginal_ram // (1024 * 1024)
        m_vram_mb = marginal_vram // (1024 * 1024)
        return ConcurrencyEvaluation(
            permitted=True,
            active_skill_count=active_count,
            max_safe_skills=max_safe_skills,
            mode=mode,
            reason=(
                f"Admitted skill sharing model '{model_id}' "
                f"(marginal RAM={m_ram_mb} MB, VRAM={m_vram_mb} MB)."
            ),
            measured_free_ram_bytes=avail_ram,
            measured_free_vram_bytes=free_vram,
        )

    def acquire_skill_lease(
        self, descriptor: SkillResourceDescriptor
    ) -> Optional[SkillExecutionLease]:
        """Attempt to acquire a skill execution lease.

        Returns:
            SkillExecutionLease if admission is granted, None otherwise.
        """
        eval_result = self.evaluate_skill_admission(descriptor)
        if not eval_result.permitted:
            return None

        # Resolve reserved RAM/VRAM
        model_id = descriptor.required_model_id
        reserved_ram = descriptor.declared_marginal_ram_bytes
        reserved_vram = descriptor.declared_marginal_vram_bytes

        if model_id is not None:
            qual = self._qualification_profiles.get(model_id)
            if qual is not None:
                reserved_ram = max(reserved_ram, qual.measured_marginal_ram_bytes)
                reserved_vram = max(reserved_vram, qual.measured_marginal_vram_bytes)

            # Ensure model is marked resident
            if not self.residency_manager.is_resident(model_id):
                self.residency_manager.set_resident(model_id, resident=True)

        lease_id = str(uuid.uuid4())
        lease = SkillExecutionLease(
            lease_id=lease_id,
            skill_id=descriptor.skill_id,
            model_id=model_id,
            reserved_ram_bytes=reserved_ram,
            reserved_vram_bytes=reserved_vram,
            is_active=True,
        )

        self._active_leases[lease_id] = lease

        # Attach lease to model in residency manager for reference counting
        if model_id is not None:
            self.residency_manager.attach_skill_lease(model_id, lease_id)

        return lease

    def release_skill_lease(self, lease: SkillExecutionLease) -> None:
        """Release an active skill execution lease upon completion."""
        lease_id = lease.lease_id
        if lease_id in self._active_leases:
            active_lease = self._active_leases.pop(lease_id)
            active_lease.is_active = False

            if active_lease.model_id is not None:
                self.residency_manager.detach_skill_lease(active_lease.model_id, lease_id)
