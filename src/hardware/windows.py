"""Windows 11 Hardware Profile Detection Provider.

Performs deterministic, read-only inspection of Windows hardware and runtime
characteristics using Python standard library (ctypes, platform, sys) and
safe machine-readable Windows commands (PowerShell CIM/JSON, nvidia-smi).
"""

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.config.models import (
    CPUProfile,
    GPUProfile,
    HardwareProfile,
    HardwareProfileState,
    OSProfile,
    RAMProfile,
    RuntimeProfile,
    StorageDriveProfile,
)
from src.hardware.detector import HardwareDetector
from src.hardware.models import (
    ComponentDiagnostic,
    DetectionStatus,
    HardwareDetectionResult,
)


class MEMORYSTATUSEX(ctypes.Structure):
    """Windows API MEMORYSTATUSEX structure for kernel32.GlobalMemoryStatusEx."""

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


def run_safe_command(
    cmd: List[str], timeout_sec: float = 5.0
) -> Tuple[bool, str, str, int, Optional[str]]:
    """Execute a system command safely without shell expansion.

    Returns:
        (success, stdout, stderr, return_code, error_message)
    """
    if not cmd:
        return False, "", "", -1, "Empty command list provided"

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return (
            res.returncode == 0,
            res.stdout.strip(),
            res.stderr.strip(),
            res.returncode,
            None if res.returncode == 0 else f"Command exited with code {res.returncode}",
        )
    except FileNotFoundError:
        return False, "", "", -1, f"Executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "", "", -1, f"Command timed out after {timeout_sec}s: {cmd[0]}"
    except Exception as exc:
        return False, "", "", -1, f"Execution failed: {type(exc).__name__}: {str(exc)}"


class WindowsHardwareDetector(HardwareDetector):
    """Deterministic, read-only hardware detector for Windows environments."""

    def __init__(self, command_timeout: float = 5.0) -> None:
        self.command_timeout = command_timeout

    def detect_os(self) -> Tuple[OSProfile, ComponentDiagnostic]:
        """Detect Windows OS version, release, build number, and architecture."""
        try:
            op_sys = platform.system() or "Windows"
            rel = platform.release() or "11"
            arch = platform.architecture()[0] or "64bit"

            build_num = "Unknown"
            if hasattr(sys, "getwindowsversion"):
                win_ver = sys.getwindowsversion()
                build_num = str(win_ver.build)
            else:
                ver_str = platform.version()
                match = re.search(r"(\d+)\.(\d+)\.(\d+)", ver_str)
                if match:
                    build_num = match.group(3)

            profile = OSProfile(
                operating_system=op_sys,
                version=rel,
                build_number=build_num,
                architecture=arch,
            )
            diag = ComponentDiagnostic(
                component="os",
                status=DetectionStatus.SUCCESS,
                provider="platform.sys.getwindowsversion",
                message="Windows operating system detected successfully",
                details={
                    "platform_system": op_sys,
                    "release": rel,
                    "build": build_num,
                    "arch": arch,
                },
            )
            return profile, diag
        except Exception as exc:
            profile = OSProfile()
            diag = ComponentDiagnostic(
                component="os",
                status=DetectionStatus.FAILED,
                provider="platform.sys.getwindowsversion",
                message=f"OS detection failed: {str(exc)}",
            )
            return profile, diag

    def detect_cpu(self) -> Tuple[CPUProfile, ComponentDiagnostic]:
        """Detect CPU topology via PowerShell CIM with standard library fallback."""
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "Get-CimInstance Win32_Processor | Select-Object Name, Manufacturer, "
                "NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed | ConvertTo-Json -Compress"
            ),
        ]
        success, stdout, stderr, code, err = run_safe_command(cmd, timeout_sec=self.command_timeout)

        if success and stdout:
            try:
                data = json.loads(stdout)
                # If multiple CPU sockets, take primary
                if isinstance(data, list) and len(data) > 0:
                    data = data[0]

                raw_vendor = str(data.get("Manufacturer", "Unknown"))
                vendor = (
                    "Intel"
                    if "Intel" in raw_vendor
                    else ("AMD" if "AMD" in raw_vendor else raw_vendor)
                )
                model = str(data.get("Name", "Unknown")).strip()
                cores = int(data.get("NumberOfCores", 1))
                log_procs = int(data.get("NumberOfLogicalProcessors", 1))
                clock = data.get("MaxClockSpeed")
                base_clock = int(clock) if clock and int(clock) >= 100 else None

                profile = CPUProfile(
                    vendor=vendor,
                    model=model,
                    physical_cores=max(1, cores),
                    logical_processors=max(1, log_procs),
                    base_clock_mhz=base_clock,
                    architecture="x86_64",
                )
                diag = ComponentDiagnostic(
                    component="cpu",
                    status=DetectionStatus.SUCCESS,
                    provider="powershell.cim.Win32_Processor",
                    message="CPU topology and properties detected successfully",
                    details={
                        "raw_vendor": raw_vendor,
                        "model": model,
                        "cores": cores,
                        "logical": log_procs,
                        "base_clock": base_clock,
                    },
                )
                return profile, diag
            except Exception as exc:
                err_msg = f"Failed to parse CPU CIM JSON: {str(exc)}"
        else:
            err_msg = err or stderr or "PowerShell CIM execution failed"

        # Fallback to standard library
        try:
            model = platform.processor() or "Unknown"
            count = os.cpu_count() or 1
            profile = CPUProfile(
                vendor="Unknown",
                model=model,
                physical_cores=max(1, count // 2),
                logical_processors=max(1, count),
                architecture="x86_64",
            )
            diag = ComponentDiagnostic(
                component="cpu",
                status=DetectionStatus.PARTIAL,
                provider="platform.os.cpu_count",
                message=f"PowerShell CIM failed ({err_msg}); fell back to stdlib approximation",
                details={"model": model, "logical_processors": count},
            )
            return profile, diag
        except Exception as exc:
            profile = CPUProfile()
            diag = ComponentDiagnostic(
                component="cpu",
                status=DetectionStatus.FAILED,
                provider="fallback",
                message=f"CPU detection failed entirely: {str(exc)}",
            )
            return profile, diag

    def detect_ram(self) -> Tuple[RAMProfile, ComponentDiagnostic]:
        """Detect physical RAM via kernel32.GlobalMemoryStatusEx with CIM fallback."""
        try:
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            res = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            if res != 0 and mem.ullTotalPhys >= 1024 * 1024 * 1024:
                profile = RAMProfile(
                    total_bytes=int(mem.ullTotalPhys),
                    available_bytes=int(mem.ullAvailPhys),
                    memory_compression_active=False,
                )
                diag = ComponentDiagnostic(
                    component="ram",
                    status=DetectionStatus.SUCCESS,
                    provider="kernel32.GlobalMemoryStatusEx",
                    message="RAM capacity and availability detected successfully",
                    details={
                        "total_bytes": int(mem.ullTotalPhys),
                        "available_bytes": int(mem.ullAvailPhys),
                        "load_percent": int(mem.dwMemoryLoad),
                    },
                )
                return profile, diag
        except Exception:
            pass

        # Fallback to PowerShell CIM
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize, "
                "FreePhysicalMemory | ConvertTo-Json -Compress"
            ),
        ]
        success, stdout, stderr, _, err = run_safe_command(cmd, timeout_sec=self.command_timeout)
        if success and stdout:
            try:
                data = json.loads(stdout)
                total_kb = int(data.get("TotalVisibleMemorySize", 0))
                free_kb = int(data.get("FreePhysicalMemory", 0))
                total_bytes = total_kb * 1024
                avail_bytes = free_kb * 1024
                if total_bytes >= 1024 * 1024 * 1024:
                    profile = RAMProfile(
                        total_bytes=total_bytes,
                        available_bytes=avail_bytes,
                        memory_compression_active=False,
                    )
                    diag = ComponentDiagnostic(
                        component="ram",
                        status=DetectionStatus.SUCCESS,
                        provider="powershell.cim.Win32_OperatingSystem",
                        message="RAM detected via PowerShell CIM fallback",
                        details={"total_bytes": total_bytes, "available_bytes": avail_bytes},
                    )
                    return profile, diag
            except Exception:
                pass

        profile = RAMProfile()
        diag = ComponentDiagnostic(
            component="ram",
            status=DetectionStatus.FAILED,
            provider="fallback",
            message="RAM detection failed across all providers; using safe default",
        )
        return profile, diag

    def detect_gpus(self) -> Tuple[List[GPUProfile], ComponentDiagnostic]:
        """Detect GPU devices via nvidia-smi (when available) and PowerShell CIM."""
        detected_gpus: List[GPUProfile] = []
        providers_used: List[str] = []

        # 1. Check optional nvidia-smi for NVIDIA GPUs
        smi_path = shutil.which("nvidia-smi")
        if smi_path:
            cmd = [
                smi_path,
                "--query-gpu=name,driver_version,memory.total,compute_cap",
                "--format=csv,noheader,nounits",
            ]
            success, stdout, _, _, _ = run_safe_command(cmd, timeout_sec=self.command_timeout)
            if success and stdout:
                providers_used.append("nvidia-smi")
                for line in stdout.splitlines():
                    parts = [p.strip() for p in line.split(",") if p.strip()]
                    if len(parts) >= 3:
                        name = parts[0]
                        driver = parts[1]
                        try:
                            vram_mib = float(parts[2])
                            vram_bytes = int(vram_mib * 1024 * 1024)
                        except ValueError:
                            vram_bytes = 0
                        compute_cap = parts[3] if len(parts) >= 4 else None

                        detected_gpus.append(
                            GPUProfile(
                                vendor="NVIDIA",
                                model=name,
                                vram_bytes=max(0, vram_bytes),
                                driver_version=driver,
                                cuda_compute_capability=compute_cap,
                            )
                        )

        # 2. Query PowerShell CIM for all video controllers
        cmd_cim = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, "
                "DriverVersion, VideoProcessor | ConvertTo-Json -Compress"
            ),
        ]
        success, stdout, _, _, _ = run_safe_command(cmd_cim, timeout_sec=self.command_timeout)
        if success and stdout:
            providers_used.append("powershell.cim.Win32_VideoController")
            try:
                raw_data = json.loads(stdout)
                items = raw_data if isinstance(raw_data, list) else [raw_data]

                existing_models = {g.model.lower() for g in detected_gpus}
                for item in items:
                    name = str(item.get("Name", "Unknown")).strip()
                    if not name or name.lower() in existing_models:
                        continue

                    # Determine vendor
                    name_lower = name.lower()
                    if "intel" in name_lower:
                        vendor = "Intel"
                    elif "amd" in name_lower or "radeon" in name_lower:
                        vendor = "AMD"
                    elif "nvidia" in name_lower:
                        vendor = "NVIDIA"
                    else:
                        vendor = "Unknown"

                    driver = str(item.get("DriverVersion", "Unknown")).strip()
                    adapter_ram = item.get("AdapterRAM")
                    vram_bytes = 0
                    if adapter_ram and isinstance(adapter_ram, (int, float)) and adapter_ram > 0:
                        vram_bytes = int(adapter_ram)

                    detected_gpus.append(
                        GPUProfile(
                            vendor=vendor,
                            model=name,
                            vram_bytes=max(0, vram_bytes),
                            driver_version=driver,
                            cuda_compute_capability=None,
                        )
                    )
            except Exception:
                pass

        if detected_gpus:
            diag = ComponentDiagnostic(
                component="gpu",
                status=DetectionStatus.SUCCESS,
                provider=",".join(providers_used),
                message=f"Discovered {len(detected_gpus)} GPU adapter(s)",
                details={
                    "count": len(detected_gpus),
                    "gpus": [g.model_dump() for g in detected_gpus],
                },
            )
            return detected_gpus, diag

        # No GPU discoverable
        unavailable_gpu = GPUProfile(
            vendor="None",
            model="Unavailable",
            vram_bytes=0,
            driver_version="N/A",
            cuda_compute_capability=None,
        )
        diag = ComponentDiagnostic(
            component="gpu",
            status=DetectionStatus.UNAVAILABLE,
            provider=",".join(providers_used) or "none",
            message="No discrete or integrated GPU detected",
        )
        return [unavailable_gpu], diag

    def detect_storage(self) -> Tuple[List[StorageDriveProfile], ComponentDiagnostic]:
        """Detect local storage volumes via PowerShell CIM with disk_usage fallback."""
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
                "Select-Object DeviceID, FileSystem, FreeSpace, Size | ConvertTo-Json -Compress"
            ),
        ]
        success, stdout, stderr, _, err = run_safe_command(cmd, timeout_sec=self.command_timeout)

        storage_drives: List[StorageDriveProfile] = []
        if success and stdout:
            try:
                data = json.loads(stdout)
                items = data if isinstance(data, list) else [data]

                for item in items:
                    dev_id = str(item.get("DeviceID", "C:")).strip()
                    fs = str(item.get("FileSystem", "NTFS")).strip()
                    size = int(item.get("Size", 0))
                    free = int(item.get("FreeSpace", 0))

                    # Filter out drives smaller than 1 GB (Pydantic schema ge=1GB)
                    if size >= 1024 * 1024 * 1024:
                        storage_drives.append(
                            StorageDriveProfile(
                                device_id=dev_id,
                                total_bytes=size,
                                free_bytes=max(0, free),
                                filesystem=fs or "NTFS",
                            )
                        )

                if storage_drives:
                    diag = ComponentDiagnostic(
                        component="storage",
                        status=DetectionStatus.SUCCESS,
                        provider="powershell.cim.Win32_LogicalDisk",
                        message=f"Detected {len(storage_drives)} local storage drive(s)",
                        details={"drives": [d.model_dump() for d in storage_drives]},
                    )
                    return storage_drives, diag
            except Exception:
                pass

        # Fallback to C: root via shutil.disk_usage
        try:
            c_root = os.path.abspath(os.path.splitdrive(os.getcwd())[0] + "\\")
            usage = shutil.disk_usage(c_root)
            if usage.total >= 1024 * 1024 * 1024:
                drive = StorageDriveProfile(
                    device_id=c_root.rstrip("\\"),
                    total_bytes=usage.total,
                    free_bytes=usage.free,
                    filesystem="NTFS",
                )
                storage_drives.append(drive)
                diag = ComponentDiagnostic(
                    component="storage",
                    status=DetectionStatus.PARTIAL,
                    provider="shutil.disk_usage",
                    message="Detected primary drive using shutil.disk_usage fallback",
                    details={"drive": drive.model_dump()},
                )
                return storage_drives, diag
        except Exception:
            pass

        # Default minimal valid drive
        default_drive = StorageDriveProfile()
        diag = ComponentDiagnostic(
            component="storage",
            status=DetectionStatus.FAILED,
            provider="fallback",
            message="Storage detection failed across all providers; using safe default",
        )
        return [default_drive], diag

    def detect_runtimes(
        self, detected_gpus: Optional[List[GPUProfile]] = None
    ) -> Tuple[RuntimeProfile, ComponentDiagnostic]:
        """Detect executing Python runtime and discoverable companion runtimes."""
        py_ver = platform.python_version()

        node_ver: Optional[str] = None
        node_path = shutil.which("node")
        if node_path:
            s, out, _, _, _ = run_safe_command([node_path, "--version"], timeout_sec=2.0)
            if s and out:
                match = re.search(r"(\d+\.\d+\.\d+)", out)
                if match:
                    node_ver = match.group(1)

        ollama_ver: Optional[str] = None
        ollama_path = shutil.which("ollama")
        if ollama_path:
            s, out, _, _, _ = run_safe_command([ollama_path, "--version"], timeout_sec=2.0)
            if s and out:
                match = re.search(r"(\d+\.\d+\.\d+)", out)
                if match:
                    ollama_ver = match.group(1)

        # Check CUDA availability
        cuda_avail = False
        if detected_gpus:
            for gpu in detected_gpus:
                if gpu.cuda_compute_capability is not None:
                    cuda_avail = True
                    break
        if not cuda_avail and shutil.which("nvcc"):
            cuda_avail = True

        profile = RuntimeProfile(
            python_version=py_ver,
            node_version=node_ver,
            cuda_available=cuda_avail,
            ollama_version=ollama_ver,
        )
        diag = ComponentDiagnostic(
            component="runtimes",
            status=DetectionStatus.SUCCESS,
            provider="system.path.inspection",
            message="Host execution runtimes detected successfully",
            details={
                "python": py_ver,
                "node": node_ver,
                "ollama": ollama_ver,
                "cuda": cuda_avail,
            },
        )
        return profile, diag

    def detect_host_identity(
        self, os_prof: OSProfile, cpu_prof: CPUProfile, ram_prof: RAMProfile
    ) -> str:
        """Compute a deterministic, non-secret host node identifier."""
        raw_seed = (
            f"{cpu_prof.vendor}|{cpu_prof.model}|{cpu_prof.physical_cores}|"
            f"{ram_prof.total_bytes}|{os_prof.operating_system}|{os_prof.architecture}"
        )
        digest = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:16]
        return f"aegis-node-{digest}"

    def detect(self) -> HardwareDetectionResult:
        """Execute complete hardware profile detection."""
        start_time = time.perf_counter()
        timestamp = datetime.now(timezone.utc).isoformat()
        diagnostics: List[ComponentDiagnostic] = []

        # 1. OS
        os_prof, os_diag = self.detect_os()
        diagnostics.append(os_diag)

        # 2. CPU
        cpu_prof, cpu_diag = self.detect_cpu()
        diagnostics.append(cpu_diag)

        # 3. RAM
        ram_prof, ram_diag = self.detect_ram()
        diagnostics.append(ram_diag)

        # 4. GPU
        gpus, gpu_diag = self.detect_gpus()
        diagnostics.append(gpu_diag)

        # Select primary GPU (prefer GPU with highest VRAM / NVIDIA acceleration)
        primary_gpu = gpus[0]
        if len(gpus) > 1:
            sorted_gpus = sorted(
                gpus,
                key=lambda g: (g.vram_bytes, 1 if g.vendor == "NVIDIA" else 0),
                reverse=True,
            )
            primary_gpu = sorted_gpus[0]

        # 5. Storage
        storage_drives, storage_diag = self.detect_storage()
        diagnostics.append(storage_diag)

        # 6. Runtimes
        runtimes_prof, runtimes_diag = self.detect_runtimes(detected_gpus=gpus)
        diagnostics.append(runtimes_diag)

        # 7. Host Identity
        host_id = self.detect_host_identity(os_prof, cpu_prof, ram_prof)

        # Construct validated HardwareProfile
        profile = HardwareProfile(
            profile_id=host_id,
            status=HardwareProfileState.REQUIRES_REBENCHMARK,
            cpu=cpu_prof,
            ram=ram_prof,
            gpu=primary_gpu,
            storage=storage_drives,
            os=os_prof,
            runtimes=runtimes_prof,
            benchmark_version="1.0",
            detection_version="1.0",
            invalidation_reason="Initial hardware detection; requires baseline benchmark",
            last_benchmarked_at=None,
        )

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        is_complete = all(d.status != DetectionStatus.FAILED for d in diagnostics)

        return HardwareDetectionResult(
            profile=profile,
            diagnostics=diagnostics,
            detected_gpus=gpus,
            detected_at=timestamp,
            detection_duration_ms=round(duration_ms, 2),
            is_complete=is_complete,
        )
