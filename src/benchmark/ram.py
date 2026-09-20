"""Deterministic RAM throughput and allocation benchmark."""

import gc
import time
from typing import Callable, List, Optional

from src.benchmark.models import BenchmarkLevel, BenchmarkStatus, PhaseResult
from src.benchmark.resources import query_memory_status
from src.benchmark.statistics import compute_cv, compute_mean, compute_median, evaluate_stability


def run_ram_benchmark(
    level: BenchmarkLevel,
    cancel_check: Optional[Callable[[], bool]] = None,
    timeout_sec: float = 10.0,
) -> PhaseResult:
    """Execute the RAM benchmark phase safely within available physical memory."""
    start_wall = time.perf_counter()
    _, avail_bytes, load_pct = query_memory_status()
    warnings: List[str] = []

    # Safe buffer sizing based on available RAM and benchmark level
    if level == BenchmarkLevel.LEVEL_0_QUICK:
        target_mb = 16
        reps = 1
    elif level == BenchmarkLevel.LEVEL_1_STANDARD:
        target_mb = 32
        reps = 3
    elif level == BenchmarkLevel.LEVEL_2_EXTENDED:
        target_mb = 64
        reps = 5
    else:  # LEVEL_3_DIAGNOSTIC
        target_mb = 128
        reps = 5

    # Check for memory pressure constraint
    constrained = False
    if avail_bytes < 800 * 1024 * 1024 or load_pct >= 90:
        constrained = True
        target_mb = min(8, target_mb)
        warnings.append(
            f"Host memory constrained (Available: {avail_bytes // (1024 * 1024)} MB, "
            f"Load: {load_pct}%); downscaled test buffer to {target_mb} MB"
        )

    buffer_bytes = target_mb * 1024 * 1024
    write_rates: List[float] = []
    read_rates: List[float] = []

    for rep in range(reps):
        if cancel_check and cancel_check():
            warnings.append("Benchmark cancelled during RAM phase")
            return PhaseResult(
                phase_name="ram",
                status=BenchmarkStatus.ABORTED,
                duration_ms=(time.perf_counter() - start_wall) * 1000.0,
                measurements={"completed_reps": rep},
                warnings=warnings,
                diagnostic_metadata={"reason": "cancelled"},
            )

        if (time.perf_counter() - start_wall) > timeout_sec:
            warnings.append(f"RAM benchmark exceeded timeout of {timeout_sec}s")
            return PhaseResult(
                phase_name="ram",
                status=BenchmarkStatus.CONSTRAINED,
                duration_ms=(time.perf_counter() - start_wall) * 1000.0,
                measurements={"completed_reps": rep},
                warnings=warnings,
                diagnostic_metadata={"reason": "timeout"},
            )

        try:
            # 1. Allocation & Write/Fill benchmark
            t_alloc_start = time.perf_counter()
            data = bytearray(buffer_bytes)
            # Memory fill (pattern write)
            pattern = b"\xaa" * 1024
            for offset in range(0, buffer_bytes, 1024):
                data[offset : offset + 1024] = pattern
            write_time = time.perf_counter() - t_alloc_start

            if write_time > 0:
                write_mb_per_sec = (buffer_bytes / (1024 * 1024)) / write_time
                write_rates.append(write_mb_per_sec)

            # 2. Sequential Read benchmark
            t_read_start = time.perf_counter()
            checksum = sum(data[::4096])
            read_time = time.perf_counter() - t_read_start

            if read_time > 0:
                read_mb_per_sec = (buffer_bytes / (1024 * 1024)) / read_time
                read_rates.append(read_mb_per_sec)

            # Release memory explicitly
            del data
            gc.collect()

        except MemoryError:
            warnings.append(f"MemoryError encountered while allocating {target_mb} MB")
            return PhaseResult(
                phase_name="ram",
                status=BenchmarkStatus.CONSTRAINED,
                duration_ms=(time.perf_counter() - start_wall) * 1000.0,
                measurements={"buffer_mb": target_mb},
                warnings=warnings,
                diagnostic_metadata={"error": "MemoryError"},
            )

    avg_write = compute_mean(write_rates)
    avg_read = compute_mean(read_rates)
    med_write = compute_median(write_rates)
    cv_write = compute_cv(write_rates)
    stability = evaluate_stability(write_rates)

    total_duration_ms = (time.perf_counter() - start_wall) * 1000.0
    status = BenchmarkStatus.CONSTRAINED if constrained else BenchmarkStatus.SUCCESS

    return PhaseResult(
        phase_name="ram",
        status=status,
        duration_ms=total_duration_ms,
        measurements={
            "write_mb_per_sec": avg_write,
            "read_mb_per_sec": avg_read,
            "median_write_mb_per_sec": med_write,
            "buffer_mb": target_mb,
            "repetitions": reps,
            "cv": cv_write,
            "stability": stability.value,
            "constrained": constrained,
            "checksum_sample": checksum if "checksum" in locals() else 0,
        },
        warnings=warnings,
        diagnostic_metadata={"target_bytes": buffer_bytes},
    )
