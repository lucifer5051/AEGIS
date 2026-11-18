"""SAGE Windows Desktop Automation Subsystem -- Configuration Model."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_ALLOWED_TOOLS: List[str] = [
    "Snapshot",
    "Screenshot",
    "DisplayInventory",
    "Invoke",
    "GetText",
    "Wait",
    "WaitFor",
    "Click",
    "Type",
    "Scroll",
    "Move",
    "Shortcut",
    "LaunchApp",
]

DEFAULT_DENIED_TOOLS: List[str] = [
    "PowerShell",
    "Registry",
    "FileSystem",
    "ProcessKill",
    "ScheduledTask",
    "Service",
    "Package",
    "Cmd",
    "Bash",
    "Net",
    "Terminal",
    "ExecuteCommand",
    "ArbitraryProcess",
]

DEFAULT_ALLOWED_APPLICATIONS: List[str] = [
    "notepad.exe",
    "calc.exe",
    "mspaint.exe",
    "chrome.exe",
    "spotify.exe",
    "antigravity.exe",
    "explorer.exe",
    "systemsettings.exe",
]


class WindowsMCPConfig(BaseModel):
    """Configuration settings for deploymenttheory/windows-mcp-server integration."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    enabled: bool = Field(
        default=False,
        description="Whether Windows MCP integration is enabled. Default False.",
    )
    executable_path: Optional[str] = Field(
        default=None,
        description="Absolute path to windows-mcp-server binary.",
    )
    startup_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=120.0,
        description="Timeout for process start and MCP initialization handshake.",
    )
    request_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Timeout for individual MCP tool calls.",
    )
    persona: str = Field(
        default="business-user",
        description="Target upstream persona baseline (default 'business-user').",
    )
    allowed_applications: List[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_APPLICATIONS),
        description="Allowlist of executable binary names permitted to be launched.",
    )
    allowed_tools: List[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS),
        description="Strict whitelist of allowed tool names.",
    )
    denied_tools: List[str] = Field(
        default_factory=lambda: list(DEFAULT_DENIED_TOOLS),
        description="Strict blacklist of forbidden destructive tool names.",
    )
    require_approval_for_actions: bool = Field(
        default=True,
        description="Whether user approval is mandatory for interactive/mutating actions.",
    )
    max_restart_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum crash restart attempts before failing closed.",
    )
    capture_evidence: bool = Field(
        default=True,
        description="Whether to capture visual or accessibility evidence for actions.",
    )

    @field_validator("executable_path")
    @classmethod
    def validate_executable_path(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip():
            path_obj = Path(v.strip())
            if not path_obj.is_absolute():
                raise ValueError(
                    f"Windows MCP executable_path must be an absolute path: '{v}'"
                )
            return str(path_obj)
        return None
