"""Storage I/O throughput and latency benchmark for AEGIS-controlled directories."""

import os
import time
import uuid
from typing import Callable, List, Optional

from src.benchmark.models import BenchmarkLevel, BenchmarkStatus, PhaseResult
from src.benchmark.statistics import compute_cv, compute_mean, compute_median, evaluate_stability


def run_storage_benchmark(
    level: BenchmarkLevel,
    target_dir: str = "runtime/scratch",
    cancel_check: Optional[Callable[[], bool]] = None,
    timeout_sec: float = 10.0,
) -> PhaseResult:
    """Execute safe, bounded storage I/O benchmark inside an AEGIS scratch directory."""
    start_wall = time.perf_counter()
    warnings: List[str] = []

    # Configure bounded file sizes and repetitions
    block_size = 64 * 1024  # 64 KB blocks
    if level == BenchmarkLevel.LEVEL_0_QUICK:
        target_mb = 4
        reps = 1
    elif level == BenchmarkLevel.LEVEL_1_STANDARD:
        target_mb = 16
        reps = 3
    elif level == BenchmarkLevel.LEVEL_2_EXTENDED:
        target_mb = 32
        reps = 5
    else:  # LEVEL_3_DIAGNOSTIC
        target_mb = 64
        reps = 5

    total_bytes = target_mb * 1024 * 1024
    blocks_count = total_bytes // block_size

    # Ensure target directory exists
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as exc:
        target_dir = os.path.abspath(".")
        warnings.append(f"Fallback to working directory: {str(exc)}")

    # Unique temporary benchmark file
    bench_filename = f"aegis_bench_{uuid.uuid4().hex[:12]}.tmp"
    bench_filepath = os.path.join(target_dir, bench_filename)

    write_rates: List[float] = []
    read_rates: List[float] = []
    block_latencies_ms: List[float] = []

    sample_block = b"\x55" * block_size

    try:
        for rep in range(reps):
            if cancel_check and cancel_check():
                warnings.append("Benchmark cancelled during storage phase")
                return PhaseResult(
                    phase_name="storage",
                    status=BenchmarkStatus.ABORTED,
                    duration_ms=(time.perf_counter() - start_wall) * 1000.0,
                    measurements={"completed_reps": rep},
                    warnings=warnings,
                    diagnostic_metadata={"reason": "cancelled"},
                )

            if (time.perf_counter() - start_wall) > timeout_sec:
                warnings.append(f"Storage benchmark exceeded timeout of {timeout_sec}s")
                return PhaseResult(
                    phase_name="storage",
                    status=BenchmarkStatus.CONSTRAINED,
                    duration_ms=(time.perf_counter() - start_wall) * 1000.0,
                    measurements={"completed_reps": rep},
                    warnings=warnings,
                    diagnostic_metadata={"reason": "timeout"},
                )

            # 1. Sequential Write Test
            t_write_start = time.perf_counter()
            with open(bench_filepath, "wb") as f:
                for _ in range(blocks_count):
                    t_block_start = time.perf_counter()
                    f.write(sample_block)
                    block_latencies_ms.append((time.perf_counter() - t_block_start) * 1000.0)
                f.flush()
                os.fsync(f.fileno())
            write_time = time.perf_counter() - t_write_start

            if write_time > 0:
                write_rates.append((total_bytes / (1024 * 1024)) / write_time)

            # 2. Sequential Read Test
            t_read_start = time.perf_counter()
            read_bytes_count = 0
            with open(bench_filepath, "rb") as f:
                while True:
                    chunk = f.read(block_size)
                    if not chunk:
                        break
                    read_bytes_count += len(chunk)
            read_time = time.perf_counter() - t_read_start

            if read_time > 0 and read_bytes_count > 0:
                read_rates.append((read_bytes_count / (1024 * 1024)) / read_time)

    except Exception as exc:
        warnings.append(f"Storage benchmark I/O error: {str(exc)}")
        return PhaseResult(
            phase_name="storage",
            status=BenchmarkStatus.FAILED,
            duration_ms=(time.perf_counter() - start_wall) * 1000.0,
            measurements={},
            warnings=warnings,
            diagnostic_metadata={"error": str(exc)},
        )
    finally:
        # Strict cleanup guarantee: verify filename prefix before deletion
        if os.path.exists(bench_filepath) and bench_filename.startswith("aegis_bench_"):
            try:
                os.remove(bench_filepath)
            except Exception:
                pass

    avg_write = compute_mean(write_rates)
    avg_read = compute_mean(read_rates)
    med_write = compute_median(write_rates)
    avg_latency = compute_mean(block_latencies_ms)
    cv_write = compute_cv(write_rates)
    stability = evaluate_stability(write_rates)

    drive_root = os.path.splitdrive(os.path.abspath(target_dir))[0] + "\\"
    total_duration_ms = (time.perf_counter() - start_wall) * 1000.0

    return PhaseResult(
        phase_name="storage",
        status=BenchmarkStatus.SUCCESS,
        duration_ms=total_duration_ms,
        measurements={
            "write_mb_per_sec": avg_write,
            "read_mb_per_sec": avg_read,
            "median_write_mb_per_sec": med_write,
            "avg_block_latency_ms": avg_latency,
            "test_file_mb": target_mb,
            "block_size_kb": block_size // 1024,
            "repetitions": reps,
            "cv": cv_write,
            "stability": stability.value,
            "drive": drive_root,
        },
        warnings=warnings,
        diagnostic_metadata={
            "temp_directory": target_dir,
            "blocks_written": blocks_count,
        },
    )
