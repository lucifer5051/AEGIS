"""Orchestrator for the SAGE Hardware Benchmark Subsystem."""

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

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
from src.benchmark.resources import (
    capture_resource_observation,
    query_memory_status,
)
from src.benchmark.storage import run_storage_benchmark
from src.config.models import HardwareProfile, HardwareProfileState


def derive_resource_capability_profile(
    metrics: NormalizedMetrics,
    peak_pressure: PressureState,
    cores: int,
    total_ram_bytes: int,
    vram_bytes: int,
) -> ResourceCapabilityProfile:
    """Synthesize multi-dimensional performance metrics into a planning profile."""
    # 1. CPU Capacity
    if metrics.cpu_ops_per_sec >= 1_500_000:
        cpu_cap = "ULTRA"
    elif metrics.cpu_ops_per_sec >= 800_000:
        cpu_cap = "HIGH"
    elif metrics.cpu_ops_per_sec >= 300_000:
        cpu_cap = "MODERATE"
    else:
        cpu_cap = "LOW"

    # 2. RAM Capacity
    ram_gb = total_ram_bytes / (1024 * 1024 * 1024)
    if ram_gb >= 30 and peak_pressure == PressureState.NORMAL:
        ram_cap = "GENEROUS"
    elif ram_gb >= 14 and peak_pressure != PressureState.RESOURCE_PRESSURE:
        ram_cap = "MODERATE"
    else:
        ram_cap = "CONSTRAINED"

    # 3. GPU Compute & VRAM
    vram_gb = vram_bytes / (1024 * 1024 * 1024)
    if vram_gb >= 12:
        gpu_vram = "HIGH_VRAM"
        gpu_compute = "HIGH_DISCRETE"
    elif vram_gb >= 5.5:
        gpu_vram = "MEDIUM_VRAM"
        gpu_compute = "MID_DISCRETE"
    elif vram_gb >= 2.0:
        gpu_vram = "LOW_VRAM"
        gpu_compute = "ENTRY_DISCRETE"
    elif vram_bytes > 0:
        gpu_vram = "LOW_VRAM"
        gpu_compute = "INTEGRATED"
    else:
        gpu_vram = "UNAVAILABLE"
        gpu_compute = "UNAVAILABLE"

    # 4. Storage Throughput & Latency
    if metrics.storage_read_mb_per_sec >= 1000:
        storage_tp = "HIGH_IOPS"
    elif metrics.storage_read_mb_per_sec >= 300:
        storage_tp = "FAST_NVME"
    else:
        storage_tp = "STANDARD"

    if metrics.storage_latency_ms < 0.5:
        storage_lat = "SUB_MILLISECOND"
    elif metrics.storage_latency_ms < 5.0:
        storage_lat = "MILLISECOND_SCALE"
    else:
        storage_lat = "HIGH_LATENCY"

    # 5. Safe Concurrency
    if peak_pressure == PressureState.RESOURCE_PRESSURE:
        safe_concurrency = 1
    elif peak_pressure == PressureState.CONSTRAINED:
        safe_concurrency = max(1, min(2, cores // 2))
    else:
        safe_concurrency = max(1, min(4, cores))

    return ResourceCapabilityProfile(
        cpu_capacity=cpu_cap,
        ram_capacity=ram_cap,
        gpu_compute=gpu_compute,
        gpu_vram=gpu_vram,
        storage_throughput=storage_tp,
        storage_latency=storage_lat,
        concurrency_capacity=safe_concurrency,
    )


def apply_benchmark_to_profile(
    profile: HardwareProfile,
    run: BenchmarkRun,
) -> HardwareProfile:
    """Transition HardwareProfile state based on verified BenchmarkRun results."""
    timestamp = datetime.now(timezone.utc).isoformat()

    valid_statuses = (
        BenchmarkStatus.SUCCESS,
        BenchmarkStatus.PARTIAL,
        BenchmarkStatus.CONSTRAINED,
    )
    if run.status in valid_statuses:
        profile.status = HardwareProfileState.VALID
        profile.last_benchmarked_at = timestamp
        profile.benchmark_version = run.benchmark_version
        profile.invalidation_reason = None
    elif run.status == BenchmarkStatus.ABORTED:
        profile.status = HardwareProfileState.REQUIRES_REBENCHMARK
        profile.invalidation_reason = "Benchmark run aborted prior to completion"
    else:  # FAILED
        profile.status = HardwareProfileState.INVALID
        profile.invalidation_reason = f"Benchmark execution failed: {', '.join(run.warnings)}"

    return profile


def run_hardware_benchmark(
    level: BenchmarkLevel = BenchmarkLevel.LEVEL_0_QUICK,
    hardware_profile: Optional[HardwareProfile] = None,
    target_dir: str = "runtime/scratch",
    cancel_check: Optional[Callable[[], bool]] = None,
    phase_timeout_sec: float = 15.0,
) -> BenchmarkRun:
    """Execute complete, bounded hardware benchmarking pipeline."""
    run_id = f"bmk-{uuid.uuid4().hex[:12]}"
    profile_id = hardware_profile.profile_id if hardware_profile else "unbound-profile"
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()

    # Track initial resource state
    _, init_avail, init_load = query_memory_status()
    min_avail = init_avail
    peak_load = init_load

    phases: Dict[str, PhaseResult] = {}
    warnings: List[str] = []

    # Phase 1: Environment Verification
    t0_env = time.perf_counter()
    phases["environment"] = PhaseResult(
        phase_name="environment",
        status=BenchmarkStatus.SUCCESS,
        duration_ms=(time.perf_counter() - t0_env) * 1000.0,
        measurements={
            "initial_memory_load_pct": init_load,
            "initial_available_ram_mb": init_avail // (1024 * 1024),
            "pid": os.getpid(),
        },
    )

    # Phase 2: CPU Benchmark
    if not (cancel_check and cancel_check()):
        phases["cpu"] = run_cpu_benchmark(
            level=level, cancel_check=cancel_check, timeout_sec=phase_timeout_sec
        )
        _, cur_avail, cur_load = query_memory_status()
        min_avail = min(min_avail, cur_avail)
        peak_load = max(peak_load, cur_load)

    # Phase 3: RAM Benchmark
    if not (cancel_check and cancel_check()):
        phases["ram"] = run_ram_benchmark(
            level=level, cancel_check=cancel_check, timeout_sec=phase_timeout_sec
        )
        _, cur_avail, cur_load = query_memory_status()
        min_avail = min(min_avail, cur_avail)
        peak_load = max(peak_load, cur_load)

    # Phase 4: GPU Benchmark (Optional)
    if not (cancel_check and cancel_check()):
        phases["gpu"] = run_gpu_benchmark(
            level=level,
            hardware_profile=hardware_profile,
            cancel_check=cancel_check,
            timeout_sec=phase_timeout_sec,
        )

    # Phase 5: Storage Benchmark
    if not (cancel_check and cancel_check()):
        phases["storage"] = run_storage_benchmark(
            level=level,
            target_dir=target_dir,
            cancel_check=cancel_check,
            timeout_sec=phase_timeout_sec,
        )
        _, cur_avail, cur_load = query_memory_status()
        min_avail = min(min_avail, cur_avail)
        peak_load = max(peak_load, cur_load)

    # Aggregate Warnings
    for p in phases.values():
        warnings.extend(p.warnings)

    # Phase 6: Resource Observation
    resource_obs = capture_resource_observation(
        initial_avail=init_avail,
        min_avail=min_avail,
        peak_load=peak_load,
        target_path=target_dir,
    )

    # Normalized Metrics Extraction
    def get_phase_meas(name: str) -> Dict[str, Any]:
        p = phases.get(name)
        return p.measurements if p else {}

    cpu_meas = get_phase_meas("cpu")
    ram_meas = get_phase_meas("ram")
    gpu_meas = get_phase_meas("gpu")
    sto_meas = get_phase_meas("storage")

    metrics = NormalizedMetrics(
        cpu_ops_per_sec=float(cpu_meas.get("ops_per_second", 0.0)),
        ram_read_mb_per_sec=float(ram_meas.get("read_mb_per_sec", 0.0)),
        ram_write_mb_per_sec=float(ram_meas.get("write_mb_per_sec", 0.0)),
        gpu_compute_tflops_proxy=gpu_meas.get("tflops_proxy"),
        gpu_vram_allocation_mb=gpu_meas.get("vram_allocation_mb"),
        storage_read_mb_per_sec=float(sto_meas.get("read_mb_per_sec", 0.0)),
        storage_write_mb_per_sec=float(sto_meas.get("write_mb_per_sec", 0.0)),
        storage_latency_ms=float(sto_meas.get("avg_block_latency_ms", 0.0)),
    )

    # Derive Resource Capability Profile
    cores = hardware_profile.cpu.physical_cores if hardware_profile else (os.cpu_count() or 1)
    tot_ram = hardware_profile.ram.total_bytes if hardware_profile else (16 * 1024 * 1024 * 1024)
    vram_bytes = hardware_profile.gpu.vram_bytes if hardware_profile else 0

    res_profile = derive_resource_capability_profile(
        metrics=metrics,
        peak_pressure=resource_obs.pressure_state,
        cores=cores,
        total_ram_bytes=tot_ram,
        vram_bytes=vram_bytes,
    )

    # Evaluate Overall Status & Stability
    if any(p.status == BenchmarkStatus.ABORTED for p in phases.values()):
        overall_status = BenchmarkStatus.ABORTED
    elif any(p.status == BenchmarkStatus.FAILED for p in phases.values()):
        overall_status = BenchmarkStatus.FAILED
    elif (
        any(p.status == BenchmarkStatus.CONSTRAINED for p in phases.values())
        or resource_obs.pressure_state == PressureState.CONSTRAINED
    ):
        overall_status = BenchmarkStatus.CONSTRAINED
    else:
        overall_status = BenchmarkStatus.SUCCESS

    # Stability calculation from cpu/ram/storage cv values
    cvs = [
        float(cpu_meas.get("cv", 0.0)),
        float(ram_meas.get("cv", 0.0)),
        float(sto_meas.get("cv", 0.0)),
    ]
    avg_cv = sum(cvs) / len(cvs) if cvs else 0.0
    if avg_cv <= 0.08:
        stability = StabilityState.STABLE
    elif avg_cv <= 0.20:
        stability = StabilityState.VARIABLE
    else:
        stability = StabilityState.CONSTRAINED

    total_duration = (time.perf_counter() - start_time) * 1000.0

    return BenchmarkRun(
        run_id=run_id,
        profile_id=profile_id,
        benchmark_version="1.0",
        benchmark_level=level,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=round(total_duration, 2),
        environment={
            "platform": "windows",
            "logical_processors": os.cpu_count() or 1,
            "level": level.value,
        },
        phases=phases,
        resource_observations=resource_obs,
        normalized_metrics=metrics,
        resource_profile=res_profile,
        stability=stability,
        status=overall_status,
        warnings=warnings,
        diagnostics={
            "initial_memory_load_pct": init_load,
            "peak_memory_load_pct": peak_load,
            "min_available_ram_mb": min_avail // (1024 * 1024),
        },
    )
