"""SAGE Model Residency Manager - Shared Model Lifecycle & Protection.

Manages model residency in system RAM/VRAM. Enables multiple concurrent skills
to share a single resident model instance without duplicate allocations.
Enforces reference counting to strictly prevent unloading models with active skill leases.
"""

from typing import Dict, List, Set


class ModelResidencyManager:
    """Coordinates resident model state and protects actively leased models."""

    def __init__(self) -> None:
        # Map of model_id -> set of active lease_ids
        self._active_leases_by_model: Dict[str, Set[str]] = {}
        # Set of currently resident model IDs
        self._resident_models: Set[str] = set()

    def is_resident(self, model_id: str) -> bool:
        """Check if a model is currently loaded in memory."""
        return model_id in self._resident_models

    def set_resident(self, model_id: str, resident: bool = True) -> None:
        """Mark a model as resident in RAM/VRAM."""
        if resident:
            self._resident_models.add(model_id)
            if model_id not in self._active_leases_by_model:
                self._active_leases_by_model[model_id] = set()
        else:
            if self.has_active_leases(model_id):
                raise RuntimeError(
                    f"Safety violation: Cannot unload model '{model_id}' "
                    f"while {len(self._active_leases_by_model[model_id])} skill leases are active."
                )
            self._resident_models.discard(model_id)
            self._active_leases_by_model.pop(model_id, None)

    def attach_skill_lease(self, model_id: str, lease_id: str) -> None:
        """Attach an active skill execution lease to a resident model."""
        if model_id not in self._resident_models:
            raise KeyError(f"Cannot attach lease to non-resident model '{model_id}'.")
        self._active_leases_by_model.setdefault(model_id, set()).add(lease_id)

    def detach_skill_lease(self, model_id: str, lease_id: str) -> None:
        """Detach a skill execution lease upon completion."""
        if model_id in self._active_leases_by_model:
            self._active_leases_by_model[model_id].discard(lease_id)

    def has_active_leases(self, model_id: str) -> bool:
        """Check whether any active skill execution leases are attached to this model."""
        return bool(self._active_leases_by_model.get(model_id))

    def get_active_lease_count(self, model_id: str) -> int:
        """Get the count of active skill execution leases attached to this model."""
        return len(self._active_leases_by_model.get(model_id, set()))

    def can_unload(self, model_id: str) -> bool:
        """Check if a model can be safely evicted.

        Returns True only if the model is resident and has ZERO active skill leases.
        """
        if model_id not in self._resident_models:
            return False
        return not self.has_active_leases(model_id)

    def unload_model(self, model_id: str) -> bool:
        """Safely evict a model from memory if no active leases exist."""
        if not self.can_unload(model_id):
            return False
        self.set_resident(model_id, resident=False)
        return True

    def list_resident_models(self) -> List[str]:
        """List all currently resident model IDs."""
        return sorted(self._resident_models)
