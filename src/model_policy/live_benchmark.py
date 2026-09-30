"""SAGE Live Hardware Benchmark Probe.

Provides deterministic, read-only inspection of live host memory (RAM via Windows
GlobalMemoryStatusEx and GPU VRAM via nvidia-smi). Used by the SkillConcurrencyManager
to calculate dynamic safe concurrency limits at runtime without relying on paper specs.
"""

import ctypes
import subprocess
from typing import Optional, Tuple


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


class LiveHardwareBenchmark:
    """Read-only live host memory and GPU probe."""

    def __init__(
        self,
        mock_avail_ram_bytes: Optional[int] = None,
        mock_total_ram_bytes: Optional[int] = None,
        mock_free_vram_bytes: Optional[int] = None,
        mock_total_vram_bytes: Optional[int] = None,
    ) -> None:
        self._mock_avail_ram_bytes = mock_avail_ram_bytes
        self._mock_total_ram_bytes = mock_total_ram_bytes
        self._mock_free_vram_bytes = mock_free_vram_bytes
        self._mock_total_vram_bytes = mock_total_vram_bytes

    def set_mock_memory(
        self,
        avail_ram_bytes: Optional[int] = None,
        free_vram_bytes: Optional[int] = None,
    ) -> None:
        """Inject mock values for deterministic testing."""
        if avail_ram_bytes is not None:
            self._mock_avail_ram_bytes = avail_ram_bytes
        if free_vram_bytes is not None:
            self._mock_free_vram_bytes = free_vram_bytes

    def query_live_memory(self) -> Tuple[int, int, int, int]:
        """Query live memory status.

        Returns:
            Tuple of (avail_ram_bytes, total_ram_bytes, free_vram_bytes, total_vram_bytes).
        """
        # If mocks injected, use them
        if self._mock_avail_ram_bytes is not None and self._mock_free_vram_bytes is not None:
            total_ram = self._mock_total_ram_bytes or (16 * 1024 * 1024 * 1024)
            total_vram = self._mock_total_vram_bytes or (6 * 1024 * 1024 * 1024)
            return (self._mock_avail_ram_bytes, total_ram, self._mock_free_vram_bytes, total_vram)

        avail_ram, total_ram = self._query_ram()
        free_vram, total_vram = self._query_vram()

        final_avail_ram = (
            self._mock_avail_ram_bytes if self._mock_avail_ram_bytes is not None else avail_ram
        )
        final_free_vram = (
            self._mock_free_vram_bytes if self._mock_free_vram_bytes is not None else free_vram
        )
        return (final_avail_ram, total_ram, final_free_vram, total_vram)

    def _query_ram(self) -> Tuple[int, int]:
        """Query Windows kernel32 GlobalMemoryStatusEx."""
        try:
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)) != 0:
                return (int(mem.ullAvailPhys), int(mem.ullTotalPhys))
        except Exception:
            pass
        # Fallback conservative baseline (3 GB free out of 16 GB)
        return (3 * 1024 * 1024 * 1024, 16 * 1024 * 1024 * 1024)

    def _query_vram(self) -> Tuple[int, int]:
        """Query nvidia-smi for GPU memory."""
        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.free,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode == 0:
                line = res.stdout.strip().split("\n")[0]
                free_str, total_str = line.split(",")
                free_mb = float(free_str.strip())
                total_mb = float(total_str.strip())
                return (int(free_mb * 1024 * 1024), int(total_mb * 1024 * 1024))
        except Exception:
            pass
        # Fallback conservative baseline (5.9 GB free out of 6 GB)
        return (int(5.9 * 1024 * 1024 * 1024), 6 * 1024 * 1024 * 1024)
