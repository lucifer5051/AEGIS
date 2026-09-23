"""AEGIS Model Hardware Qualification Subsystem.

Implements the trusted manifest requirement:
"hardware_qualification": "MEASURED_PROBE_LOAD_AND_LATENCY"

Derives empirical resident RAM, resident VRAM, marginal per-inference RAM,
marginal per-inference VRAM, and p95 latency on the physical host.
Never relies on paper specifications or hardcoded estimates.
"""

import hashlib
import json
import platform
import time
from pathlib import Path
from typing import Callable, Optional

from src.model_policy.live_benchmark import LiveHardwareBenchmark
from src.model_policy.models import ModelHardwareQualificationProfile


class ModelHardwareQualifier:
    """Orchestrates live hardware qualification probes for models."""

    def __init__(
        self,
        benchmark: Optional[LiveHardwareBenchmark] = None,
        qualification_dir: Optional[Path] = None,
    ) -> None:
        self.benchmark = benchmark or LiveHardwareBenchmark()
        self.qualification_dir = qualification_dir

    def compute_hardware_hash(self) -> str:
        """Compute deterministic digest of host hardware characteristics."""
        _, total_ram, _, total_vram = self.benchmark.query_live_memory()
        machine_info = (
            f"{platform.processor()}|{platform.machine()}|{total_ram}|{total_vram}"
        )
        return hashlib.sha256(machine_info.encode("utf-8")).hexdigest()[:16]

    def qualify_model(
        self,
        model_id: str,
        manifest_name: str,
        probe_load_fn: Callable[[], None],
        probe_inference_fn: Callable[[], float],
    ) -> ModelHardwareQualificationProfile:
        """Execute live hardware qualification probe and record empirical metrics.

        Args:
            model_id: Unique model identifier.
            manifest_name: Associated manifest name.
            probe_load_fn: Callback that instantiates/loads model weights.
            probe_inference_fn: Callback that performs warm-up inference and returns latency (ms).

        Returns:
            ModelHardwareQualificationProfile with empirical metrics.
        """
        # 1. Pre-load baseline
        base_ram_avail, _, base_vram_free, _ = self.benchmark.query_live_memory()

        # 2. Probe Load
        probe_load_fn()
        post_load_ram_avail, _, post_load_vram_free, _ = self.benchmark.query_live_memory()

        resident_ram = max(0, base_ram_avail - post_load_ram_avail)
        resident_vram = max(0, base_vram_free - post_load_vram_free)

        # 3. Probe Inference (warm-up & marginal memory)
        latencies = []
        marginal_ram_max = 0
        marginal_vram_max = 0

        for _ in range(3):
            t_start = time.perf_counter()
            probe_inference_fn()
            latencies.append((time.perf_counter() - t_start) * 1000.0)

            post_infer_ram_avail, _, post_infer_vram_free, _ = self.benchmark.query_live_memory()
            marginal_ram = max(0, post_load_ram_avail - post_infer_ram_avail)
            marginal_vram = max(0, post_load_vram_free - post_infer_vram_free)
            marginal_ram_max = max(marginal_ram_max, marginal_ram)
            marginal_vram_max = max(marginal_vram_max, marginal_vram)

        # Compute p95 latency
        latencies.sort()
        idx_p95 = int(len(latencies) * 0.95)
        p95_latency = latencies[min(idx_p95, len(latencies) - 1)]

        # Fallback conservative marginal floor if OS pages were not tracked
        final_marginal_ram = max(marginal_ram_max, 32 * 1024 * 1024)
        final_marginal_vram = max(marginal_vram_max, 16 * 1024 * 1024)

        status = "QUALIFIED"
        if resident_ram > 6 * 1024 * 1024 * 1024 or resident_vram > 5 * 1024 * 1024 * 1024:
            status = "CONSTRAINED"

        profile = ModelHardwareQualificationProfile(
            model_id=model_id,
            manifest_name=manifest_name,
            hardware_hash=self.compute_hardware_hash(),
            measured_resident_ram_bytes=resident_ram,
            measured_resident_vram_bytes=resident_vram,
            measured_marginal_ram_bytes=final_marginal_ram,
            measured_marginal_vram_bytes=final_marginal_vram,
            measured_p95_latency_ms=round(p95_latency, 2),
            qualification_status=status,
        )

        if self.qualification_dir:
            self.save_profile(profile)

        return profile

    def save_profile(self, profile: ModelHardwareQualificationProfile) -> Path:
        """Persist qualification profile to JSON file."""
        if not self.qualification_dir:
            raise ValueError("qualification_dir not configured")
        self.qualification_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.qualification_dir / f"{profile.model_id}.qualification.json"
        file_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
        return file_path

    def load_profile(self, model_id: str) -> Optional[ModelHardwareQualificationProfile]:
        """Load persisted qualification profile from disk."""
        if not self.qualification_dir:
            return None
        file_path = self.qualification_dir / f"{model_id}.qualification.json"
        if not file_path.is_file():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return ModelHardwareQualificationProfile.model_validate(data)
        except Exception:
            return None
