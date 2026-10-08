"""SAGE Step Observation Model.

Captures empirical post-execution observation and state change metadata.
Follows invariant: do not pretend observation data exists when it was not actually collected.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.tools.models import ToolResult, ToolResultStatus


class ObservationStatus(str, Enum):
    """Empirical status of the executed tool."""

    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    DENIED = "DENIED"
    TIMEOUT = "TIMEOUT"


class StepObservation(BaseModel):
    """Explicit observation collected directly after tool execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_number: int = Field(ge=1, description="Step sequence index.")
    tool_name: str = Field(description="Name of the tool invoked.")
    status: ObservationStatus = Field(description="Exit status of the tool execution.")
    exit_code: int = Field(default=0, description="Process/tool exit code (0 = normal).")
    output_summary: str = Field(description="Sanitized summary or snippet of tool output.")
    raw_output: str = Field(description="Sanitized complete output.")
    duration_ms: float = Field(ge=0.0, default=0.0, description="Execution elapsed time.")
    artifacts_created: List[str] = Field(
        default_factory=list, description="Paths of created or modified artifacts."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Deterministic tool result metadata."
    )

    @classmethod
    def from_tool_result(
        cls,
        step_number: int,
        tool_name: str,
        result: ToolResult,
        artifacts: Optional[List[str]] = None,
    ) -> StepObservation:
        """Create StepObservation directly from a broker ToolResult."""
        status_map = {
            ToolResultStatus.SUCCESS: ObservationStatus.SUCCESS,
            ToolResultStatus.ERROR: ObservationStatus.ERROR,
            ToolResultStatus.DENIED: ObservationStatus.DENIED,
            ToolResultStatus.TIMEOUT: ObservationStatus.TIMEOUT,
        }
        obs_status = status_map.get(result.status, ObservationStatus.ERROR)
        exit_code = 0 if result.status == ToolResultStatus.SUCCESS else 1

        summary = result.output.splitlines()[0] if result.output else ""
        if len(summary) > 200:
            summary = summary[:197] + "..."

        return cls(
            step_number=step_number,
            tool_name=tool_name,
            status=obs_status,
            exit_code=exit_code,
            output_summary=summary,
            raw_output=result.output,
            duration_ms=getattr(result, "duration_ms", 0.0),
            artifacts_created=artifacts or [],
            metadata={
                "tool_name": tool_name,
                "sanitized": getattr(result, "is_sanitized", False),
                "status": result.status.value,
            },
        )
