"""Diagnostic and detection result models for the Hardware Profile Subsystem.

Reuses canonical HardwareProfile and sub-models from src.config.models.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from src.config.models import (
    AegisBaseConfig,
    GPUProfile,
    HardwareProfile,
)


class DetectionStatus(str, Enum):
    """Detection outcome status for individual hardware components."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class ComponentDiagnostic(AegisBaseConfig):
    """Structured diagnostic result for a detected hardware/runtime component."""

    component: str = Field(..., description="Target hardware or runtime component name.")
    status: DetectionStatus = Field(..., description="Detection outcome status.")
    provider: str = Field(..., description="Underlying provider or inspection method used.")
    message: Optional[str] = Field(default=None, description="Diagnostic human-readable message.")
    details: Dict[str, Any] = Field(
        default_factory=dict, description="Sanitized diagnostic metadata."
    )


class HardwareDetectionResult(AegisBaseConfig):
    """Encapsulates the complete hardware detection outcome and diagnostic trail."""

    profile: HardwareProfile = Field(..., description="Canonical validated HardwareProfile object.")
    diagnostics: List[ComponentDiagnostic] = Field(
        default_factory=list, description="Component-level diagnostics."
    )
    detected_gpus: List[GPUProfile] = Field(
        default_factory=list, description="All discovered GPU devices on the system."
    )
    detected_at: str = Field(..., description="ISO 8601 UTC timestamp when detection occurred.")
    detection_duration_ms: float = Field(
        ..., ge=0.0, description="Elapsed execution time in milliseconds."
    )
    is_complete: bool = Field(
        default=True, description="True if core components were detected without failure."
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert detection result to Python dictionary."""
        return self.model_dump()

    def to_json(self, indent: int = 2) -> str:
        """Serialize detection result to formatted JSON string."""
        return self.model_dump_json(indent=indent)
