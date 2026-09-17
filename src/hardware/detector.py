"""Abstract interface and convenience entry point for hardware profile detection."""

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from src.config.models import (
    CPUProfile,
    GPUProfile,
    OSProfile,
    RAMProfile,
    RuntimeProfile,
    StorageDriveProfile,
)
from src.hardware.models import ComponentDiagnostic, HardwareDetectionResult


class HardwareDetector(ABC):
    """Abstract interface for platform-specific hardware profile detection."""

    @abstractmethod
    def detect_os(self) -> Tuple[OSProfile, ComponentDiagnostic]:
        """Detect operating system name, version, architecture, and build."""
        ...

    @abstractmethod
    def detect_cpu(self) -> Tuple[CPUProfile, ComponentDiagnostic]:
        """Detect CPU vendor, model, physical cores, logical processors, and frequency."""
        ...

    @abstractmethod
    def detect_ram(self) -> Tuple[RAMProfile, ComponentDiagnostic]:
        """Detect physical RAM capacity and available memory."""
        ...

    @abstractmethod
    def detect_gpus(self) -> Tuple[List[GPUProfile], ComponentDiagnostic]:
        """Detect all installed GPU adapters and acceleration metadata."""
        ...

    @abstractmethod
    def detect_storage(self) -> Tuple[List[StorageDriveProfile], ComponentDiagnostic]:
        """Detect relevant local storage volumes."""
        ...

    @abstractmethod
    def detect_runtimes(self) -> Tuple[RuntimeProfile, ComponentDiagnostic]:
        """Detect host execution runtimes (Python, Node, Ollama, CUDA)."""
        ...

    @abstractmethod
    def detect_host_identity(
        self, os_prof: OSProfile, cpu_prof: CPUProfile, ram_prof: RAMProfile
    ) -> str:
        """Compute a non-secret, deterministic host profile identifier."""
        ...

    @abstractmethod
    def detect(self) -> HardwareDetectionResult:
        """Execute complete hardware detection and return validated result."""
        ...


def detect_hardware_profile(
    detector: Optional[HardwareDetector] = None,
) -> HardwareDetectionResult:
    """Convenience entry point executing hardware profile detection.

    Defaults to WindowsHardwareDetector on Windows platforms.
    """
    if detector is None:
        from src.hardware.windows import WindowsHardwareDetector

        detector = WindowsHardwareDetector()
    return detector.detect()
