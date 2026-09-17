"""AEGIS Hardware Profile Detection Subsystem.

Deterministic, read-only hardware inspection producing validated HardwareProfile
models conforming to the canonical AEGIS architecture.
"""

from src.hardware.detector import HardwareDetector, detect_hardware_profile
from src.hardware.models import (
    ComponentDiagnostic,
    DetectionStatus,
    HardwareDetectionResult,
)
from src.hardware.windows import WindowsHardwareDetector

__all__ = [
    "HardwareDetector",
    "WindowsHardwareDetector",
    "detect_hardware_profile",
    "DetectionStatus",
    "ComponentDiagnostic",
    "HardwareDetectionResult",
]
