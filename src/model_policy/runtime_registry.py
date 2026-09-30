"""SAGE Model Runtime Registry - Framework Deduplication & Inventory.

Maintains an authoritative registry of framework runtimes (PyTorch, ONNX, llama.cpp)
and models. Prevents duplicate accounting when multiple models share a single runtime.
"""

from typing import Dict, List, Optional

from src.model_policy.models import (
    ModelArtifactDescriptor,
    ModelRuntimeDescriptor,
    StorageBudgetStatus,
)


class ModelRuntimeRegistry:
    """Authoritative registry of runtimes and model artifacts."""

    def __init__(self) -> None:
        self._runtimes: Dict[str, ModelRuntimeDescriptor] = {}
        self._models: Dict[str, ModelArtifactDescriptor] = {}

    def register_runtime(self, runtime: ModelRuntimeDescriptor) -> None:
        """Register an installable framework runtime."""
        self._runtimes[runtime.runtime_id] = runtime

    def register_model(self, model: ModelArtifactDescriptor) -> None:
        """Register a model artifact descriptor.

        Raises:
            KeyError: If the model's referenced runtime_id is not registered.
        """
        if model.runtime_id not in self._runtimes:
            raise KeyError(
                f"Cannot register model '{model.model_id}': runtime '{model.runtime_id}' "
                f"is not registered."
            )
        self._models[model.model_id] = model

    def get_runtime(self, runtime_id: str) -> Optional[ModelRuntimeDescriptor]:
        """Look up a runtime by identifier."""
        return self._runtimes.get(runtime_id)

    def get_model(self, model_id: str) -> Optional[ModelArtifactDescriptor]:
        """Look up a model artifact descriptor by identifier."""
        return self._models.get(model_id)

    def list_runtimes(self) -> List[ModelRuntimeDescriptor]:
        """List all registered runtimes."""
        return list(self._runtimes.values())

    def list_models(self) -> List[ModelArtifactDescriptor]:
        """List all registered model artifact descriptors."""
        return list(self._models.values())

    def calculate_deduplicated_runtime_footprint(
        self, runtime_ids: Optional[List[str]] = None
    ) -> int:
        """Calculate the total installed disk footprint of runtimes, counting shared ones once.

        Args:
            runtime_ids: Subset of runtime IDs to sum. If None, sums all registered runtimes.

        Returns:
            Total disk bytes of unique runtimes.
        """
        target_ids = set(runtime_ids) if runtime_ids is not None else set(self._runtimes.keys())
        total_bytes = 0
        for rid in target_ids:
            runtime = self._runtimes.get(rid)
            if runtime is not None:
                total_bytes += runtime.installed_disk_bytes
        return total_bytes

    def calculate_total_artifact_footprint(self, model_ids: Optional[List[str]] = None) -> int:
        """Calculate the exact sum of pinned artifact byte sizes.

        Args:
            model_ids: Subset of model IDs to sum. If None, sums all registered models.

        Returns:
            Total artifact bytes.
        """
        target_ids = set(model_ids) if model_ids is not None else set(self._models.keys())
        total_bytes = 0
        for mid in target_ids:
            model = self._models.get(mid)
            if model is not None:
                total_bytes += model.exact_artifact_bytes
        return total_bytes

    def get_storage_budget_status(
        self,
        total_budget_bytes: int,
        host_free_bytes: int,
        host_reserve_floor_bytes: int,
        installed_runtime_ids: Optional[List[str]] = None,
        installed_model_ids: Optional[List[str]] = None,
    ) -> StorageBudgetStatus:
        """Compute the current global storage budget consumption."""
        consumed_runtimes = self.calculate_deduplicated_runtime_footprint(installed_runtime_ids)
        consumed_artifacts = self.calculate_total_artifact_footprint(installed_model_ids)
        total_consumed = consumed_runtimes + consumed_artifacts
        remaining = max(0, total_budget_bytes - total_consumed)

        return StorageBudgetStatus(
            total_budget_bytes=total_budget_bytes,
            consumed_runtime_bytes=consumed_runtimes,
            consumed_artifact_bytes=consumed_artifacts,
            remaining_budget_bytes=remaining,
            host_free_bytes=host_free_bytes,
            host_reserve_floor_bytes=host_reserve_floor_bytes,
        )
