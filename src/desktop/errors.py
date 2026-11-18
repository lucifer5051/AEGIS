"""SAGE Windows Desktop Automation Subsystem -- Errors."""

from __future__ import annotations


class DesktopAutomationError(Exception):
    """Base exception for desktop automation errors."""


class DesktopSecurityError(DesktopAutomationError):
    """Raised when an operation violates security boundaries or policy constraints."""


class DesktopTimeoutError(DesktopAutomationError):
    """Raised when an MCP operation or process startup times out."""


class DesktopProcessError(DesktopAutomationError):
    """Raised when the external MCP server process crashes, fails to start, or exits unexpectedly."""


class ToolManifestValidationError(DesktopAutomationError):
    """Raised when the server advertises forbidden or unexpected tools."""
