"""Resource observation and pressure detection for benchmarking."""

import ctypes
import os
import shutil
from typing import Tuple

from src.benchmark.models import PressureState, ResourceObservation


class MEMORYSTATUSEX(ctypes.Structure):
    """Windows API MEMORYSTATUSEX structure for memory queries."""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def query_memory_status() -> Tuple[int, int, int]:
    """Query Windows memory status: (total_bytes, available_bytes, load_percent)."""
    try:
        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)) != 0:
            return (int(mem.ullTotalPhys), int(mem.ullAvailPhys), int(mem.dwMemoryLoad))
    except Exception:
        pass
    # Fallback default (16 GB, 2 GB free, 85% load)
    return (16 * 1024 * 1024 * 1024, 2 * 1024 * 1024 * 1024, 85)


def query_storage_free_bytes(path: str = ".") -> int:
    """Query free bytes for the drive containing path."""
    try:
        abs_path = os.path.abspath(path)
        root = os.path.splitdrive(abs_path)[0] + "\\"
        return shutil.disk_usage(root).free
    except Exception:
        return 50 * 1024 * 1024 * 1024


def evaluate_pressure(available_bytes: int, load_percent: int) -> PressureState:
    """Determine resource pressure state based on memory parameters."""
    # Under 500 MB free or > 95% load is severe pressure
    if available_bytes < 500 * 1024 * 1024 or load_percent >= 95:
        return PressureState.RESOURCE_PRESSURE
    # Under 1.5 GB free or > 88% load is constrained
    if available_bytes < 1536 * 1024 * 1024 or load_percent >= 88:
        return PressureState.CONSTRAINED
    return PressureState.NORMAL


def capture_resource_observation(
    initial_avail: int,
    min_avail: int,
    peak_load: int,
    target_path: str = ".",
) -> ResourceObservation:
    """Construct a validated ResourceObservation instance."""
    pressure = evaluate_pressure(min_avail, peak_load)
    storage_free = query_storage_free_bytes(target_path)

    return ResourceObservation(
        initial_ram_avail_bytes=initial_avail,
        min_ram_avail_bytes=min_avail,
        peak_memory_load_percent=peak_load,
        pressure_state=pressure,
        storage_free_bytes=storage_free,
        temperature_celsius=None,  # Reported as unavailable without invasive drivers
    )
