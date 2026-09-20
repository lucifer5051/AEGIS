"""AEGIS Hardware Benchmark Subsystem.

Deterministic, controlled hardware benchmarking characterizing CPU, RAM, GPU,
storage, and resource pressure to transition hardware profiles from
REQUIRES_REBENCHMARK to VALID.
"""

from src.benchmark.cpu import run_cpu_benchmark
from src.benchmark.gpu import run_gpu_benchmark
from src.benchmark.models import (
    BenchmarkLevel,
    BenchmarkRun,
    BenchmarkStatus,
    NormalizedMetrics,
    PhaseResult,
    PressureState,
    ResourceCapabilityProfile,
    StabilityState,
)
from src.benchmark.ram import run_ram_benchmark
from src.benchmark.runner import apply_benchmark_to_profile, run_hardware_benchmark
from src.benchmark.storage import run_storage_benchmark

__all__ = [
    "BenchmarkLevel",
    "BenchmarkStatus",
    "StabilityState",
    "PressureState",
    "PhaseResult",
    "NormalizedMetrics",
    "ResourceCapabilityProfile",
    "BenchmarkRun",
    "run_hardware_benchmark",
    "apply_benchmark_to_profile",
    "run_cpu_benchmark",
    "run_ram_benchmark",
    "run_gpu_benchmark",
    "run_storage_benchmark",
]
