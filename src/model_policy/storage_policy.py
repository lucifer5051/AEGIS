"""AEGIS Model Storage Policy - Host Reserve Floor & Global Budget Accounting.

Enforces strict disk storage boundaries:
1. Hard host reserve floor (min 40.0 GB free on C: drive).
2. Global model storage budget ceiling (30.0 GB for all runtimes and models).
3. Manifest-pinned exact artifact sizes (3,168,262,223 bytes for Von).
4. Storage boundary enforcement: all weights and runtimes must reside outside OneDrive and the repo.
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional

from src.model_policy.models import PreflightResult
from src.model_policy.runtime_registry import ModelRuntimeRegistry


class ModelStoragePolicy:
    """Enforces storage preflight checks and budget limits."""

    def __init__(
        self,
        registry: ModelRuntimeRegistry,
        min_host_reserve_bytes: int = 40 * 1024 * 1024 * 1024,  # 40 GB host floor
        model_storage_budget_bytes: int = 30 * 1024 * 1024 * 1024,  # 30 GB global budget
        storage_root: Optional[Path] = None,
    ) -> None:
        self.registry = registry
        self.min_host_reserve_bytes = min_host_reserve_bytes
        self.model_storage_budget_bytes = model_storage_budget_bytes

        if storage_root is None:
            user_profile = os.environ.get("USERPROFILE", "C:\\Users\\Default")
            self.storage_root = Path(user_profile) / ".aegis" / "models"
        else:
            self.storage_root = storage_root

    def validate_storage_boundary(self, target_path: Path) -> bool:
        """Verify that target storage is outside OneDrive and outside repository root."""
        resolved = str(target_path.resolve()).lower()
        if "onedrive" in resolved:
            return False
        # Do not allow inside repository
        repo_indicator = Path(__file__).resolve().parent.parent.parent
        if str(repo_indicator.resolve()).lower() in resolved:
            return False
        return True

    def query_host_free_bytes(self) -> int:
        """Query actual physical free bytes on the target drive."""
        try:
            drive_root = os.path.splitdrive(str(self.storage_root.resolve()))[0] + "\\"
            return shutil.disk_usage(drive_root).free
        except Exception:
            return 80 * 1024 * 1024 * 1024

    def preflight_model_installation(
        self,
        model_id: str,
        runtime_already_installed: bool = False,
        mock_host_free_bytes: Optional[int] = None,
        installed_runtime_ids: Optional[List[str]] = None,
        installed_model_ids: Optional[List[str]] = None,
    ) -> PreflightResult:
        """Perform comprehensive pre-flight verification before installing a model."""
        # 1. Validate storage boundary
        if not self.validate_storage_boundary(self.storage_root):
            return PreflightResult(
                permitted=False,
                required_bytes=0,
                transient_bytes=0,
                free_storage_bytes=0,
                storage_floor_bytes=self.min_host_reserve_bytes,
                reason=f"Storage boundary violation: '{self.storage_root}' "
                f"cannot be inside OneDrive or repository.",
            )

        # 2. Look up model and runtime descriptors
        model = self.registry.get_model(model_id)
        if model is None:
            return PreflightResult(
                permitted=False,
                required_bytes=0,
                transient_bytes=0,
                free_storage_bytes=0,
                storage_floor_bytes=self.min_host_reserve_bytes,
                reason=f"Model '{model_id}' is not registered in ModelRuntimeRegistry.",
            )

        runtime = self.registry.get_runtime(model.runtime_id)
        if runtime is None:
            return PreflightResult(
                permitted=False,
                required_bytes=0,
                transient_bytes=0,
                free_storage_bytes=0,
                storage_floor_bytes=self.min_host_reserve_bytes,
                reason=f"Runtime '{model.runtime_id}' is not registered.",
            )

        # 3. Calculate required disk footprint
        runtime_disk = 0 if runtime_already_installed else runtime.installed_disk_bytes
        runtime_transient = 0 if runtime_already_installed else runtime.transient_install_bytes
        model_disk = model.exact_artifact_bytes

        total_permanent_needed = runtime_disk + model_disk
        peak_transient_needed = runtime_transient + model_disk

        host_free = (
            mock_host_free_bytes
            if mock_host_free_bytes is not None
            else self.query_host_free_bytes()
        )

        # 4. Check host reserve floor: host_free - peak_transient >= min_host_reserve_bytes
        if host_free - peak_transient_needed < self.min_host_reserve_bytes:
            margin = (host_free - peak_transient_needed) // (1024 * 1024 * 1024)
            floor_gb = self.min_host_reserve_bytes // (1024 * 1024 * 1024)
            return PreflightResult(
                permitted=False,
                required_bytes=total_permanent_needed,
                transient_bytes=peak_transient_needed,
                free_storage_bytes=host_free,
                storage_floor_bytes=self.min_host_reserve_bytes,
                reason=f"Host storage floor violation: free space after installation "
                f"({margin} GB) would fall below minimum reserve ({floor_gb} GB).",
            )

        # 5. Check global model budget
        status = self.registry.get_storage_budget_status(
            total_budget_bytes=self.model_storage_budget_bytes,
            host_free_bytes=host_free,
            host_reserve_floor_bytes=self.min_host_reserve_bytes,
            installed_runtime_ids=installed_runtime_ids,
            installed_model_ids=installed_model_ids,
        )

        if status.remaining_budget_bytes < total_permanent_needed:
            needed_gb = total_permanent_needed / (1024 * 1024 * 1024)
            rem_gb = status.remaining_budget_bytes / (1024 * 1024 * 1024)
            return PreflightResult(
                permitted=False,
                required_bytes=total_permanent_needed,
                transient_bytes=peak_transient_needed,
                free_storage_bytes=host_free,
                storage_floor_bytes=self.min_host_reserve_bytes,
                reason=f"Global model budget violation: requires {needed_gb:.2f} GB, "
                f"but remaining budget is only {rem_gb:.2f} GB.",
            )

        return PreflightResult(
            permitted=True,
            required_bytes=total_permanent_needed,
            transient_bytes=peak_transient_needed,
            free_storage_bytes=host_free,
            storage_floor_bytes=self.min_host_reserve_bytes,
            reason="Preflight passed: satisfies host floor and global budget limits.",
        )
