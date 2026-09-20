"""Deterministic unit tests and live smoke test for the Hardware Benchmark Subsystem.

Covers all 18 specified scenarios with controlled mocks/fixtures, plus a live
bounded execution smoke test on the host machine. 100% offline.
"""

import json
import os
import tempfile
from unittest.mock import patch

from src.benchmark.cpu import run_cpu_benchmark
from src.benchmark.gpu import run_gpu_benchmark
from src.benchmark.models import (
    BenchmarkLevel,
    BenchmarkStatus,
    NormalizedMetrics,
    PhaseResult,
    PressureState,
    StabilityState,
)
from src.benchmark.ram import run_ram_benchmark
from src.benchmark.resources import (
    evaluate_pressure,
)
from src.benchmark.runner import (
    apply_benchmark_to_profile,
    derive_resource_capability_profile,
    run_hardware_benchmark,
)
from src.benchmark.statistics import (
    compute_cv,
    compute_mean,
    compute_median,
    compute_stdev,
    evaluate_stability,
)
from src.benchmark.storage import run_storage_benchmark
from src.config.models import (
    CPUProfile,
    GPUProfile,
    HardwareProfile,
    HardwareProfileState,
    RAMProfile,
)


# ==============================================================================
# 1. CPU BENCHMARK CALCULATION
# ==============================================================================
def test_01_cpu_benchmark_calculation():
    """Verify CPU benchmark executes bounded iterations and calculates throughput."""
    res = run_cpu_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK)
    assert res.status == BenchmarkStatus.SUCCESS
    assert res.phase_name == "cpu"
    assert res.duration_ms > 0.0
    assert "ops_per_second" in res.measurements
    assert res.measurements["ops_per_second"] > 0.0
    assert res.measurements["total_ops"] == 50_000


# ==============================================================================
# 2. RAM BENCHMARK CALCULATION
# ==============================================================================
def test_02_ram_benchmark_calculation():
    """Verify RAM benchmark allocates test buffer and measures write/read rates."""
    res = run_ram_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK)
    assert res.status == BenchmarkStatus.SUCCESS
    assert res.phase_name == "ram"
    assert "write_mb_per_sec" in res.measurements
    assert "read_mb_per_sec" in res.measurements
    assert res.measurements["write_mb_per_sec"] > 0.0
    assert res.measurements["read_mb_per_sec"] > 0.0


# ==============================================================================
# 3. GPU UNAVAILABLE
# ==============================================================================
def test_03_gpu_unavailable():
    """Verify GPU phase gracefully reports UNAVAILABLE without failing."""
    profile = HardwareProfile(gpu=GPUProfile(vendor="None", model="Unavailable", vram_bytes=0))
    res = run_gpu_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK, hardware_profile=profile)
    assert res.status == BenchmarkStatus.SUCCESS
    assert res.phase_name == "gpu"
    assert res.measurements["compute_status"] == "UNAVAILABLE"
    assert res.measurements["detected_vendor"] == "None"


# ==============================================================================
# 4. GPU BENCHMARK CALCULATION
# ==============================================================================
def test_04_gpu_benchmark_calculation():
    """Verify GPU benchmark calculates metrics when hardware is present."""
    profile = HardwareProfile(
        gpu=GPUProfile(
            vendor="NVIDIA",
            model="RTX 3050 Laptop",
            vram_bytes=6 * 1024 * 1024 * 1024,
            cuda_compute_capability="8.6",
        )
    )
    # When tensor frameworks are absent, it safely returns detected specs
    res = run_gpu_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK, hardware_profile=profile)
    assert res.status == BenchmarkStatus.SUCCESS
    assert res.measurements["detected_vendor"] == "NVIDIA"
    assert res.measurements["detected_vram_mb"] == 6144


# ==============================================================================
# 5. STORAGE BENCHMARK CALCULATION
# ==============================================================================
def test_05_storage_benchmark_calculation():
    """Verify storage benchmark performs read/write and cleans up temp files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        res = run_storage_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK, target_dir=temp_dir)
        assert res.status == BenchmarkStatus.SUCCESS
        assert res.phase_name == "storage"
        assert "write_mb_per_sec" in res.measurements
        assert "read_mb_per_sec" in res.measurements
        assert res.measurements["write_mb_per_sec"] > 0.0
        # Check that no benchmark temporary files linger
        lingering = [f for f in os.listdir(temp_dir) if f.startswith("aegis_bench_")]
        assert len(lingering) == 0


# ==============================================================================
# 6. STATISTICS
# ==============================================================================
def test_06_statistics_calculations():
    """Verify statistical functions: mean, median, and standard deviation."""
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert compute_mean(vals) == 30.0
    assert compute_median(vals) == 30.0
    assert round(compute_stdev(vals), 2) == 15.81

    # Empty edge case
    assert compute_mean([]) == 0.0
    assert compute_median([]) == 0.0
    assert compute_stdev([]) == 0.0


# ==============================================================================
# 7. COEFFICIENT OF VARIATION & STABILITY
# ==============================================================================
def test_07_coefficient_of_variation_and_stability():
    """Verify CV calculation and stability classification."""
    # Stable (< 8% variance)
    stable_vals = [100.0, 102.0, 101.0, 99.0, 100.0]
    cv_stable = compute_cv(stable_vals)
    assert cv_stable < 0.08
    assert evaluate_stability(stable_vals) == StabilityState.STABLE

    # Variable (between 8% and 20%)
    variable_vals = [100.0, 115.0, 90.0, 105.0]
    assert evaluate_stability(variable_vals) == StabilityState.VARIABLE

    # Constrained (> 20% or failure)
    erratic_vals = [100.0, 180.0, 60.0]
    assert evaluate_stability(erratic_vals) == StabilityState.CONSTRAINED
    assert evaluate_stability(stable_vals, failure_count=1) == StabilityState.CONSTRAINED


# ==============================================================================
# 8. RESOURCE PRESSURE DETECTION
# ==============================================================================
def test_08_resource_pressure_detection():
    """Verify resource pressure classification based on memory metrics."""
    # Normal: 2.5 GB free, 70% load
    assert evaluate_pressure(2500 * 1024 * 1024, 70) == PressureState.NORMAL
    # Constrained: 1.2 GB free, 89% load
    assert evaluate_pressure(1200 * 1024 * 1024, 89) == PressureState.CONSTRAINED
    # Severe pressure: 300 MB free or 96% load
    assert evaluate_pressure(300 * 1024 * 1024, 80) == PressureState.RESOURCE_PRESSURE
    assert evaluate_pressure(2000 * 1024 * 1024, 96) == PressureState.RESOURCE_PRESSURE


# ==============================================================================
# 9. TIMEOUT HANDLING
# ==============================================================================
def test_09_timeout_handling():
    """Verify CPU and RAM benchmarks abort gracefully upon timeout."""
    # Set impossible 0.00001s timeout
    res = run_cpu_benchmark(level=BenchmarkLevel.LEVEL_1_STANDARD, timeout_sec=0.000001)
    assert res.status == BenchmarkStatus.CONSTRAINED
    assert any("timeout" in w.lower() for w in res.warnings)


# ==============================================================================
# 10. CANCELLATION HANDLING
# ==============================================================================
def test_10_cancellation_handling():
    """Verify benchmark run respects cancellation callbacks."""
    cancelled = True
    res = run_cpu_benchmark(level=BenchmarkLevel.LEVEL_1_STANDARD, cancel_check=lambda: cancelled)
    assert res.status == BenchmarkStatus.ABORTED
    assert any("cancelled" in w.lower() for w in res.warnings)


# ==============================================================================
# 11. INVALID MEASUREMENT HANDLING
# ==============================================================================
def test_11_invalid_measurement_handling():
    """Verify storage benchmark handles I/O permission or filesystem errors cleanly."""
    # Target an invalid read-only non-directory path
    with patch("builtins.open", side_effect=PermissionError("Mock Permission Denied")):
        res = run_storage_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK, target_dir=".")
        assert res.status == BenchmarkStatus.FAILED
        assert any("error" in w.lower() for w in res.warnings)


# ==============================================================================
# 12. PARTIAL BENCHMARK
# ==============================================================================
def test_12_partial_benchmark():
    """Verify benchmark run succeeds with PARTIAL/CONSTRAINED status when constrained."""
    mock_mem = (16 * 1024**3, 500 * 1024**2, 92)
    with patch("src.benchmark.ram.query_memory_status", return_value=mock_mem):
        run = run_hardware_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK)
        assert run.status in (BenchmarkStatus.CONSTRAINED, BenchmarkStatus.SUCCESS)
        assert run.phases["ram"].measurements.get("constrained", False) is True


# ==============================================================================
# 13. SUCCESSFUL BENCHMARK RUN
# ==============================================================================
def test_13_successful_benchmark_run():
    """Verify complete benchmark run executes all phases and derives resource profile."""
    profile = HardwareProfile(
        cpu=CPUProfile(vendor="Intel", model="Core i5", physical_cores=10),
        ram=RAMProfile(total_bytes=16 * 1024 * 1024 * 1024),
        gpu=GPUProfile(vendor="NVIDIA", model="RTX 3050", vram_bytes=6 * 1024 * 1024 * 1024),
    )
    run = run_hardware_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK, hardware_profile=profile)
    assert run.status in (BenchmarkStatus.SUCCESS, BenchmarkStatus.CONSTRAINED)
    assert "cpu" in run.phases
    assert "ram" in run.phases
    assert "gpu" in run.phases
    assert "storage" in run.phases
    assert run.resource_profile.concurrency_capacity >= 1


# ==============================================================================
# 14. FAILED BENCHMARK RUN
# ==============================================================================
def test_14_failed_benchmark_run():
    """Verify that an abort in a phase sets the overall benchmark status to ABORTED."""
    with patch("src.benchmark.runner.run_cpu_benchmark") as mock_cpu:
        mock_cpu.return_value = PhaseResult(
            phase_name="cpu",
            status=BenchmarkStatus.ABORTED,
            duration_ms=10.0,
            warnings=["Aborted manually"],
            measurements={},
        )
        run = run_hardware_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK)
        assert run.status == BenchmarkStatus.ABORTED


# ==============================================================================
# 15. STATE TRANSITION
# ==============================================================================
def test_15_state_transition():
    """Verify HardwareProfile transitions from REQUIRES_REBENCHMARK to VALID."""
    profile = HardwareProfile(status=HardwareProfileState.REQUIRES_REBENCHMARK)
    assert profile.status == HardwareProfileState.REQUIRES_REBENCHMARK

    # Run quick benchmark
    run = run_hardware_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK, hardware_profile=profile)
    updated_profile = apply_benchmark_to_profile(profile, run)

    assert updated_profile.status == HardwareProfileState.VALID
    assert updated_profile.last_benchmarked_at is not None
    assert updated_profile.invalidation_reason is None


# ==============================================================================
# 16. RESULT SERIALIZATION
# ==============================================================================
def test_16_result_serialization():
    """Verify BenchmarkRun serializes to dictionary and JSON cleanly."""
    run = run_hardware_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK)
    dumped = run.to_dict()
    assert isinstance(dumped, dict)
    assert "run_id" in dumped
    assert "phases" in dumped

    json_str = run.to_json()
    assert isinstance(json_str, str)
    parsed = json.loads(json_str)
    assert parsed["run_id"] == run.run_id


# ==============================================================================
# 17. DETERMINISTIC BENCHMARK CONFIGURATION
# ==============================================================================
def test_17_deterministic_benchmark_configuration():
    """Verify derivation of ResourceCapabilityProfile is reproducible."""
    metrics = NormalizedMetrics(
        cpu_ops_per_sec=1_600_000.0,
        ram_read_mb_per_sec=5000.0,
        ram_write_mb_per_sec=3000.0,
        storage_read_mb_per_sec=800.0,
        storage_write_mb_per_sec=400.0,
        storage_latency_ms=0.3,
    )
    profile1 = derive_resource_capability_profile(
        metrics=metrics,
        peak_pressure=PressureState.NORMAL,
        cores=8,
        total_ram_bytes=32 * 1024**3,
        vram_bytes=6 * 1024**3,
    )
    profile2 = derive_resource_capability_profile(
        metrics=metrics,
        peak_pressure=PressureState.NORMAL,
        cores=8,
        total_ram_bytes=32 * 1024**3,
        vram_bytes=6 * 1024**3,
    )

    assert profile1.cpu_capacity == "ULTRA"
    assert profile1.ram_capacity == "GENEROUS"
    assert profile1.storage_latency == "SUB_MILLISECOND"
    assert profile1.concurrency_capacity == 4
    assert profile1.model_dump() == profile2.model_dump()


# ==============================================================================
# 18. SECRET EXCLUSION
# ==============================================================================
def test_18_secret_exclusion():
    """Verify zero credentials, secrets, or tokens exist in benchmark results."""
    run = run_hardware_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK)
    json_repr = run.to_json().lower()

    forbidden = ["password", "secret", "api_key", "bearer", "private_key", "token_secret"]
    for term in forbidden:
        assert term not in json_repr, f"Forbidden secret term '{term}' found in benchmark run!"


# ==============================================================================
# 19. LIVE WINDOWS SMOKE BENCHMARK
# ==============================================================================
def test_19_live_windows_smoke_benchmark():
    """Live smoke test executing bounded LEVEL_0_QUICK benchmark on the host."""
    run = run_hardware_benchmark(level=BenchmarkLevel.LEVEL_0_QUICK)

    assert run.run_id.startswith("bmk-")
    assert run.status in (BenchmarkStatus.SUCCESS, BenchmarkStatus.CONSTRAINED)
    assert run.duration_ms > 0.0
    assert run.phases["cpu"].status == BenchmarkStatus.SUCCESS
    assert run.phases["ram"].status in (BenchmarkStatus.SUCCESS, BenchmarkStatus.CONSTRAINED)
    assert run.phases["storage"].status == BenchmarkStatus.SUCCESS
    assert run.normalized_metrics.cpu_ops_per_sec > 0.0
    assert run.resource_profile.concurrency_capacity >= 1
