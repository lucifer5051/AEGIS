"""Typed data models for the SAGE Hardware Benchmark Subsystem.

Defines benchmark modes, execution states, phase results, statistical
aggregations, normalized metrics, and derived resource capability profiles.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from src.config.models import AegisBaseConfig


class BenchmarkLevel(str, Enum):
    """Configurable benchmarking depth levels."""

    LEVEL_0_QUICK = "LEVEL_0_QUICK"
    LEVEL_1_STANDARD = "LEVEL_1_STANDARD"
    LEVEL_2_EXTENDED = "LEVEL_2_EXTENDED"
    LEVEL_3_DIAGNOSTIC = "LEVEL_3_DIAGNOSTIC"


class BenchmarkStatus(str, Enum):
    """Lifecycle and execution status of benchmark runs and phases."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    CONSTRAINED = "CONSTRAINED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


class StabilityState(str, Enum):
    """Statistical stability classification across repeated measurements."""

    STABLE = "STABLE"
    VARIABLE = "VARIABLE"
    CONSTRAINED = "CONSTRAINED"
    INCONCLUSIVE = "INCONCLUSIVE"


class PressureState(str, Enum):
    """Hardware resource pressure classification."""

    NORMAL = "NORMAL"
    CONSTRAINED = "CONSTRAINED"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    ABORTED = "ABORTED"


class PhaseResult(AegisBaseConfig):
    """Structured outcome for an individual benchmark phase."""

    phase_name: str = Field(..., description="Name of the benchmark phase.")
    status: BenchmarkStatus = Field(..., description="Phase execution status.")
    duration_ms: float = Field(..., ge=0.0, description="Duration in milliseconds.")
    measurements: Dict[str, Any] = Field(
        default_factory=dict, description="Raw and derived measurements."
    )
    warnings: List[str] = Field(default_factory=list, description="Warnings encountered.")
    diagnostic_metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Diagnostic context."
    )


class ResourceObservation(AegisBaseConfig):
    """Observed resource metrics before and during benchmark execution."""

    initial_ram_avail_bytes: int = Field(..., ge=0)
    min_ram_avail_bytes: int = Field(..., ge=0)
    peak_memory_load_percent: int = Field(..., ge=0, le=100)
    pressure_state: PressureState = Field(default=PressureState.NORMAL)
    storage_free_bytes: int = Field(..., ge=0)
    temperature_celsius: Optional[float] = Field(default=None)


class NormalizedMetrics(AegisBaseConfig):
    """Normalized multi-dimensional performance metrics for planning."""

    cpu_ops_per_sec: float = Field(default=0.0, ge=0.0)
    ram_read_mb_per_sec: float = Field(default=0.0, ge=0.0)
    ram_write_mb_per_sec: float = Field(default=0.0, ge=0.0)
    gpu_compute_tflops_proxy: Optional[float] = Field(default=None, ge=0.0)
    gpu_vram_allocation_mb: Optional[int] = Field(default=None, ge=0)
    storage_read_mb_per_sec: float = Field(default=0.0, ge=0.0)
    storage_write_mb_per_sec: float = Field(default=0.0, ge=0.0)
    storage_latency_ms: float = Field(default=0.0, ge=0.0)


class ResourceCapabilityProfile(AegisBaseConfig):
    """Derived resource capability profile for future provisioning and scheduling."""

    cpu_capacity: str = Field(default="MODERATE")
    ram_capacity: str = Field(default="MODERATE")
    gpu_compute: str = Field(default="UNAVAILABLE")
    gpu_vram: str = Field(default="UNAVAILABLE")
    storage_throughput: str = Field(default="STANDARD")
    storage_latency: str = Field(default="MILLISECOND_SCALE")
    concurrency_capacity: int = Field(default=2, ge=1)


class BenchmarkRun(AegisBaseConfig):
    """Top-level record capturing a complete hardware benchmark run."""

    run_id: str = Field(..., description="Unique run identifier.")
    profile_id: str = Field(..., description="Associated HardwareProfile ID.")
    benchmark_version: str = Field(default="1.0")
    benchmark_level: BenchmarkLevel = Field(default=BenchmarkLevel.LEVEL_0_QUICK)
    started_at: str = Field(..., description="ISO 8601 UTC start timestamp.")
    completed_at: Optional[str] = Field(default=None)
    duration_ms: float = Field(default=0.0, ge=0.0)
    environment: Dict[str, Any] = Field(default_factory=dict)
    phases: Dict[str, PhaseResult] = Field(default_factory=dict)
    resource_observations: ResourceObservation
    normalized_metrics: NormalizedMetrics
    resource_profile: ResourceCapabilityProfile
    stability: StabilityState = Field(default=StabilityState.STABLE)
    status: BenchmarkStatus = Field(default=BenchmarkStatus.PENDING)
    warnings: List[str] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize benchmark run to Python dictionary."""
        return self.model_dump()

    def to_json(self, indent: int = 2) -> str:
        """Serialize benchmark run to JSON string."""
        return self.model_dump_json(indent=indent)
