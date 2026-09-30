"""Deterministic CPU micro-benchmark for SAGE resource characterization."""

import hashlib
import os
import time
from typing import Callable, List, Optional

from src.benchmark.models import BenchmarkLevel, BenchmarkStatus, PhaseResult
from src.benchmark.statistics import compute_cv, compute_mean, compute_median, evaluate_stability


def run_cpu_workload(iterations: int, cancel_check: Optional[Callable[[], bool]] = None) -> int:
    """Execute a deterministic, CPU-bound workload combining hashing and arithmetic."""
    ops = 0
    state = 0x12345678
    hasher = hashlib.sha256()

    for i in range(iterations):
        if cancel_check and cancel_check():
            break
        # Deterministic integer arithmetic + bitwise mixing
        state = ((state * 1103515245 + 12345) & 0x7FFFFFFF) ^ (i & 0xFF)
        hasher.update(state.to_bytes(4, byteorder="little"))
        ops += 1

    return ops


def run_cpu_benchmark(
    level: BenchmarkLevel,
    cancel_check: Optional[Callable[[], bool]] = None,
    timeout_sec: float = 10.0,
) -> PhaseResult:
    """Execute the CPU benchmark phase according to the configured benchmark level."""
    start_wall = time.perf_counter()

    # Configure iterations and repetitions by level
    if level == BenchmarkLevel.LEVEL_0_QUICK:
        reps = 1
        iterations_per_rep = 50_000
    elif level == BenchmarkLevel.LEVEL_1_STANDARD:
        reps = 3
        iterations_per_rep = 150_000
    elif level == BenchmarkLevel.LEVEL_2_EXTENDED:
        reps = 5
        iterations_per_rep = 250_000
    else:  # LEVEL_3_DIAGNOSTIC
        reps = 5
        iterations_per_rep = 400_000

    rates: List[float] = []
    durations: List[float] = []
    total_ops = 0
    warnings: List[str] = []

    for rep in range(reps):
        if cancel_check and cancel_check():
            warnings.append("Benchmark cancelled during CPU phase")
            return PhaseResult(
                phase_name="cpu",
                status=BenchmarkStatus.ABORTED,
                duration_ms=(time.perf_counter() - start_wall) * 1000.0,
                measurements={"completed_reps": rep},
                warnings=warnings,
                diagnostic_metadata={"reason": "cancelled"},
            )

        if (time.perf_counter() - start_wall) > timeout_sec:
            warnings.append(f"CPU benchmark exceeded timeout of {timeout_sec}s")
            return PhaseResult(
                phase_name="cpu",
                status=BenchmarkStatus.CONSTRAINED,
                duration_ms=(time.perf_counter() - start_wall) * 1000.0,
                measurements={"completed_reps": rep},
                warnings=warnings,
                diagnostic_metadata={"reason": "timeout"},
            )

        t0 = time.perf_counter()
        ops = run_cpu_workload(iterations_per_rep, cancel_check=cancel_check)
        elapsed = time.perf_counter() - t0

        if elapsed > 0:
            rate = ops / elapsed
            rates.append(rate)
            durations.append(elapsed * 1000.0)
            total_ops += ops

    stability = evaluate_stability(rates)
    cv = compute_cv(rates)
    avg_rate = compute_mean(rates)
    med_rate = compute_median(rates)
    total_duration_ms = (time.perf_counter() - start_wall) * 1000.0

    return PhaseResult(
        phase_name="cpu",
        status=BenchmarkStatus.SUCCESS,
        duration_ms=total_duration_ms,
        measurements={
            "ops_per_second": avg_rate,
            "median_ops_per_second": med_rate,
            "total_ops": total_ops,
            "cv": cv,
            "stability": stability.value,
            "repetitions": reps,
            "rep_rates": rates,
            "logical_processors": os.cpu_count() or 1,
        },
        warnings=warnings,
        diagnostic_metadata={
            "workload_type": "sha256_integer_arithmetic",
            "iterations_per_rep": iterations_per_rep,
        },
    )
