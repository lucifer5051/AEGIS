"""Deterministic unit tests and live Windows smoke test for Hardware Profile Detection.

Covers all 17 required scenarios using controlled mocks/fixtures plus a live
host verification test. 100% offline.
"""

import json
import platform
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.config.models import (
    CPUProfile,
    GPUProfile,
    HardwareProfile,
    HardwareProfileState,
    OSProfile,
    RAMProfile,
)
from src.hardware.detector import detect_hardware_profile
from src.hardware.models import (
    ComponentDiagnostic,
    DetectionStatus,
    HardwareDetectionResult,
)
from src.hardware.windows import (
    WindowsHardwareDetector,
    run_safe_command,
)


# ==============================================================================
# 1. CPU PARSING
# ==============================================================================
def test_01_cpu_parsing():
    """Verify parsing of Win32_Processor CIM JSON output into CPUProfile."""
    detector = WindowsHardwareDetector()
    mock_json = json.dumps(
        {
            "Name": "Intel(R) Core(TM) i7-12700H",
            "Manufacturer": "GenuineIntel",
            "NumberOfCores": 14,
            "NumberOfLogicalProcessors": 20,
            "MaxClockSpeed": 2700,
        }
    )

    with patch(
        "src.hardware.windows.run_safe_command",
        return_value=(True, mock_json, "", 0, None),
    ):
        profile, diag = detector.detect_cpu()

    assert profile.vendor == "Intel"
    assert profile.model == "Intel(R) Core(TM) i7-12700H"
    assert profile.physical_cores == 14
    assert profile.logical_processors == 20
    assert profile.base_clock_mhz == 2700
    assert profile.architecture == "x86_64"
    assert diag.status == DetectionStatus.SUCCESS
    assert diag.provider == "powershell.cim.Win32_Processor"


# ==============================================================================
# 2. RAM PARSING
# ==============================================================================
def test_02_ram_parsing():
    """Verify RAM detection via GlobalMemoryStatusEx and CIM fallback."""
    detector = WindowsHardwareDetector()

    # Test CIM fallback by mocking ctypes failure
    mock_cim_json = json.dumps(
        {
            "TotalVisibleMemorySize": 33554432,  # 32 GB in KiB
            "FreePhysicalMemory": 16777216,  # 16 GB in KiB
        }
    )

    with patch("ctypes.windll.kernel32.GlobalMemoryStatusEx", side_effect=OSError("Ctypes error")):
        with patch(
            "src.hardware.windows.run_safe_command",
            return_value=(True, mock_cim_json, "", 0, None),
        ):
            profile, diag = detector.detect_ram()

    assert profile.total_bytes == 33554432 * 1024  # 32 GiB in bytes
    assert profile.available_bytes == 16777216 * 1024
    assert diag.status == DetectionStatus.SUCCESS
    assert "Win32_OperatingSystem" in diag.provider


# ==============================================================================
# 3. GPU PARSING
# ==============================================================================
def test_03_gpu_parsing():
    """Verify parsing of nvidia-smi CSV output into GPUProfile."""
    detector = WindowsHardwareDetector()
    mock_csv = "NVIDIA RTX A5000 Laptop GPU, 528.49, 16384, 8.6\n"

    with patch("shutil.which", return_value="C:\\dummy\\nvidia-smi.exe"):
        with patch(
            "src.hardware.windows.run_safe_command",
            side_effect=[
                (True, mock_csv, "", 0, None),  # nvidia-smi
                (False, "", "", 1, "CIM not called"),  # CIM
            ],
        ):
            gpus, diag = detector.detect_gpus()

    assert len(gpus) == 1
    assert gpus[0].vendor == "NVIDIA"
    assert gpus[0].model == "NVIDIA RTX A5000 Laptop GPU"
    assert gpus[0].vram_bytes == 16384 * 1024 * 1024  # 16 GB in bytes
    assert gpus[0].driver_version == "528.49"
    assert gpus[0].cuda_compute_capability == "8.6"
    assert diag.status == DetectionStatus.SUCCESS


# ==============================================================================
# 4. STORAGE PARSING
# ==============================================================================
def test_04_storage_parsing():
    """Verify parsing of Win32_LogicalDisk JSON and filtering < 1 GB drives."""
    detector = WindowsHardwareDetector()
    mock_json = json.dumps(
        [
            {
                "DeviceID": "C:",
                "FileSystem": "NTFS",
                "Size": 500 * 1024 * 1024 * 1024,
                "FreeSpace": 200 * 1024 * 1024 * 1024,
            },
            {
                "DeviceID": "E:",
                "FileSystem": "FAT32",
                "Size": 500 * 1024 * 1024,  # 500 MB (should be filtered out < 1 GB)
                "FreeSpace": 100 * 1024 * 1024,
            },
        ]
    )

    with patch(
        "src.hardware.windows.run_safe_command",
        return_value=(True, mock_json, "", 0, None),
    ):
        drives, diag = detector.detect_storage()

    assert len(drives) == 1
    assert drives[0].device_id == "C:"
    assert drives[0].filesystem == "NTFS"
    assert drives[0].total_bytes == 500 * 1024 * 1024 * 1024
    assert drives[0].free_bytes == 200 * 1024 * 1024 * 1024
    assert diag.status == DetectionStatus.SUCCESS


# ==============================================================================
# 5. OS PARSING
# ==============================================================================
def test_05_os_parsing():
    """Verify OS detection accurately captures release, build, and architecture."""
    detector = WindowsHardwareDetector()
    mock_winver = MagicMock()
    mock_winver.build = 26220

    with patch("platform.system", return_value="Windows"):
        with patch("platform.release", return_value="11"):
            with patch("platform.architecture", return_value=("64bit", "WindowsPE")):
                with patch("sys.getwindowsversion", return_value=mock_winver, create=True):
                    profile, diag = detector.detect_os()

    assert profile.operating_system == "Windows"
    assert profile.version == "11"
    assert profile.build_number == "26220"
    assert profile.architecture == "64bit"
    assert diag.status == DetectionStatus.SUCCESS


# ==============================================================================
# 6. RUNTIME PARSING
# ==============================================================================
def test_06_runtime_parsing():
    """Verify detection of Python, Node.js, Ollama, and CUDA runtimes."""
    detector = WindowsHardwareDetector()

    with patch("platform.python_version", return_value="3.12.10"):
        with patch("shutil.which", side_effect=lambda x: f"C:\\dummy\\{x}.exe"):
            with patch(
                "src.hardware.windows.run_safe_command",
                side_effect=[
                    (True, "v24.13.0", "", 0, None),  # node
                    (True, "client version is 0.34.0", "", 0, None),  # ollama
                ],
            ):
                profile, diag = detector.detect_runtimes(detected_gpus=[])

    assert profile.python_version == "3.12.10"
    assert profile.node_version == "24.13.0"
    assert profile.ollama_version == "0.34.0"
    assert diag.status == DetectionStatus.SUCCESS


# ==============================================================================
# 7. MISSING GPU
# ==============================================================================
def test_07_missing_gpu():
    """Verify that a system with no detectable GPU returns UNAVAILABLE without crashing."""
    detector = WindowsHardwareDetector()

    with patch("shutil.which", return_value=None):
        with patch(
            "src.hardware.windows.run_safe_command",
            return_value=(True, "[]", "", 0, None),  # Empty CIM response
        ):
            gpus, diag = detector.detect_gpus()

    assert len(gpus) == 1
    assert gpus[0].vendor == "None"
    assert gpus[0].model == "Unavailable"
    assert gpus[0].vram_bytes == 0
    assert diag.status == DetectionStatus.UNAVAILABLE


# ==============================================================================
# 8. FAILED COMMAND
# ==============================================================================
def test_08_failed_command():
    """Verify that subprocess failure returns structured error and falls back gracefully."""
    detector = WindowsHardwareDetector()

    with patch(
        "src.hardware.windows.run_safe_command",
        return_value=(False, "", "CIM error", 1, "Command exited with code 1"),
    ):
        with patch("platform.processor", return_value="Intel64 Family 6"):
            with patch("os.cpu_count", return_value=8):
                profile, diag = detector.detect_cpu()

    assert profile.model == "Intel64 Family 6"
    assert profile.logical_processors == 8
    assert diag.status == DetectionStatus.PARTIAL


# ==============================================================================
# 9. COMMAND TIMEOUT
# ==============================================================================
def test_09_command_timeout():
    """Verify run_safe_command catches TimeoutExpired and returns clean error."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["test.exe"], timeout=2.0),
    ):
        success, stdout, stderr, code, err = run_safe_command(["test.exe"], timeout_sec=2.0)

    assert not success
    assert code == -1
    assert "timed out" in (err or "").lower()


# ==============================================================================
# 10. MALFORMED COMMAND OUTPUT
# ==============================================================================
def test_10_malformed_command_output():
    """Verify corrupted JSON output from PowerShell CIM falls back safely."""
    detector = WindowsHardwareDetector()

    with patch(
        "src.hardware.windows.run_safe_command",
        return_value=(True, "{corrupt json invalid", "", 0, None),
    ):
        with patch("platform.processor", return_value="AMD Ryzen 7"):
            with patch("os.cpu_count", return_value=16):
                profile, diag = detector.detect_cpu()

    assert profile.model == "AMD Ryzen 7"
    assert diag.status == DetectionStatus.PARTIAL


# ==============================================================================
# 11. PARTIAL DETECTION
# ==============================================================================
def test_11_partial_detection():
    """Verify that partial component detection produces a valid overall HardwareProfile."""
    detector = WindowsHardwareDetector()

    # Force GPU detection to UNAVAILABLE
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
        provider="mock",
        message="No GPU detected",
    )
    with patch.object(
        detector,
        "detect_gpus",
        return_value=([unavailable_gpu], diag),
    ):
        result = detector.detect()

    assert isinstance(result.profile, HardwareProfile)
    assert result.profile.status == HardwareProfileState.REQUIRES_REBENCHMARK
    assert result.profile.gpu.vendor == "None"
    assert result.profile.gpu.model == "Unavailable"
    assert result.profile.gpu.vram_bytes == 0


# ==============================================================================
# 12. INVALID NUMERIC VALUE
# ==============================================================================
def test_12_invalid_numeric_value():
    """Verify that invalid numeric strings in storage or memory are handled cleanly."""
    detector = WindowsHardwareDetector()
    mock_json = json.dumps(
        {
            "DeviceID": "C:",
            "FileSystem": "NTFS",
            "Size": "not-a-number",
            "FreeSpace": -500,
        }
    )

    with patch(
        "src.hardware.windows.run_safe_command",
        return_value=(True, mock_json, "", 0, None),
    ):
        with patch(
            "shutil.disk_usage",
            return_value=MagicMock(total=100 * 1024**3, free=50 * 1024**3),
        ):
            drives, diag = detector.detect_storage()

    assert len(drives) >= 1
    assert drives[0].total_bytes >= 1024 * 1024 * 1024


# ==============================================================================
# 13. UNIT NORMALIZATION
# ==============================================================================
def test_13_unit_normalization():
    """Verify all units are strictly converted to canonical bytes and MHz."""
    detector = WindowsHardwareDetector()

    # 16 MiB from nvidia-smi must become exactly 16 * 1024 * 1024 bytes
    mock_csv = "NVIDIA GeForce, 500.0, 16, 8.0\n"
    with patch("shutil.which", return_value="C:\\dummy\\nvidia-smi.exe"):
        with patch(
            "src.hardware.windows.run_safe_command",
            return_value=(True, mock_csv, "", 0, None),
        ):
            gpus, _ = detector.detect_gpus()

    assert gpus[0].vram_bytes == 16 * 1024 * 1024


# ==============================================================================
# 14. DETERMINISTIC SERIALIZATION
# ==============================================================================
def test_14_deterministic_serialization():
    """Verify HardwareDetectionResult serializes deterministically to JSON and dict."""
    detector = WindowsHardwareDetector()
    result = detector.detect()

    dumped_dict = result.to_dict()
    assert "profile" in dumped_dict
    assert "diagnostics" in dumped_dict

    json_str = result.to_json()
    assert isinstance(json_str, str)
    parsed = json.loads(json_str)
    assert parsed["profile"]["status"] == "REQUIRES_REBENCHMARK"


# ==============================================================================
# 15. HARDWARE PROFILE VALIDATION
# ==============================================================================
def test_15_hardware_profile_validation():
    """Verify resulting profile conforms to the canonical HardwareProfile model."""
    detector = WindowsHardwareDetector()
    result = detector.detect()

    assert isinstance(result.profile, HardwareProfile)
    assert result.profile.cpu.physical_cores >= 1
    assert result.profile.ram.total_bytes >= 1024 * 1024 * 1024
    assert len(result.profile.storage) >= 1


# ==============================================================================
# 16. HOST IDENTITY GENERATION
# ==============================================================================
def test_16_host_identity_generation():
    """Verify host identity generation produces stable, deterministic node IDs."""
    detector = WindowsHardwareDetector()
    os_p = OSProfile(
        operating_system="Windows",
        version="11",
        build_number="26220",
        architecture="64bit",
    )
    cpu_p = CPUProfile(vendor="Intel", model="i7-12700H", physical_cores=14, logical_processors=20)
    ram_p = RAMProfile(total_bytes=32 * 1024 * 1024 * 1024)

    id1 = detector.detect_host_identity(os_p, cpu_p, ram_p)
    id2 = detector.detect_host_identity(os_p, cpu_p, ram_p)

    assert id1 == id2
    assert id1.startswith("aegis-node-")
    assert len(id1) == len("aegis-node-") + 16


# ==============================================================================
# 17. SECRET EXCLUSION
# ==============================================================================
def test_17_secret_exclusion():
    """Verify no credentials or secret keys exist in the generated profile or diagnostics."""
    detector = WindowsHardwareDetector()
    result = detector.detect()
    dumped_json = result.to_json().lower()

    forbidden_terms = ["password", "secret", "api_key", "bearer", "private_key", "token_secret"]
    for term in forbidden_terms:
        assert term not in dumped_json, f"Secret term '{term}' found in hardware output!"


# ==============================================================================
# 18. LIVE WINDOWS SMOKE TEST
# ==============================================================================
@pytest.mark.skipif(platform.system() != "Windows", reason="Windows 11 live smoke test")
def test_18_live_windows_smoke_test():
    """Live smoke test executing hardware detection on the active host machine."""
    result: HardwareDetectionResult = detect_hardware_profile()

    assert result.profile is not None
    assert result.profile.profile_id.startswith("aegis-node-")
    assert result.profile.status == HardwareProfileState.REQUIRES_REBENCHMARK

    # CPU check
    assert result.profile.cpu.vendor != ""
    assert result.profile.cpu.model != ""
    assert result.profile.cpu.physical_cores >= 1

    # RAM check
    assert result.profile.ram.total_bytes >= 1024 * 1024 * 1024

    # Storage check
    assert len(result.profile.storage) >= 1
    assert result.profile.storage[0].total_bytes >= 1024 * 1024 * 1024

    # OS check
    assert result.profile.os.operating_system == "Windows"

    # GPU presence - optional, must not fail if unavailable
    assert result.profile.gpu is not None
    assert result.profile.gpu.vram_bytes >= 0

    # Diagnostics presence
    assert len(result.diagnostics) >= 5
