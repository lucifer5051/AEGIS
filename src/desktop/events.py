"""SAGE Desktop Awareness Subsystem -- Event & Schema Models.

Canonical Reference:
    PROMPT 074A / PROMPT 075: SAGE Desktop Awareness Subsystem
"""

from __future__ import annotations

import time
from enum import Enum, unique
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


@unique
class DesktopEventType(str, Enum):
    """Normalized desktop event categories emitted by the native observer."""

    OBSERVER_READY = "OBSERVER_READY"
    FOREGROUND_CHANGED = "FOREGROUND_CHANGED"
    FOCUS_CHANGED = "FOCUS_CHANGED"
    WINDOW_OPENED = "WINDOW_OPENED"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
    PROPERTY_CHANGED = "PROPERTY_CHANGED"
    HEARTBEAT = "HEARTBEAT"
    INTERRUPT_DETECTED = "INTERRUPT_DETECTED"
    GENERIC_WIN_EVENT = "WIN_EVENT"


class EventBounds(BaseModel):
    """Bounding screen coordinates of a window or UI element."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


class DesktopEvent(BaseModel):
    """Normalized, compact event record emitted by sage-desktop-watcher."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: str = Field(default="1.0", description="Protocol version.")
    event_id: str = Field(default_factory=lambda: uuid4().hex, description="Unique event identifier.")
    event_type: str = Field(description="Normalized event category.")
    timestamp: float = Field(default_factory=time.time, description="UTC unix epoch timestamp.")
    process_id: int = Field(default=0, description="Process ID of the emitting application.")
    window_handle: int = Field(default=0, description="Win32 HWND integer.")
    session_generation: int = Field(default=1, description="Observer session generation number.")
    process_name: str = Field(default="", description="Base executable name (e.g. notepad.exe).")
    window_title: str = Field(default="", description="Window title text.")
    element_id: Optional[str] = Field(default=None, description="AutomationId if available.")
    control_type: Optional[str] = Field(default=None, description="UIA ControlType name.")
    runtime_id: Optional[List[int]] = Field(default=None, description="Native UIA runtime ID array.")
    bounds: Optional[EventBounds] = Field(default=None, description="Bounding screen rectangle.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Optional diagnostic attributes.")


class UIElementNode(BaseModel):
    """Structured hierarchical UI Automation node returned from targeted inspect queries."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    class_name: str = ""
    is_enabled: bool = True
    native_window_handle: int = 0
    process_id: int = 0
    runtime_id: Optional[List[int]] = None
    bounds: Optional[EventBounds] = None
    patterns: List[str] = Field(default_factory=list)
    children: List[UIElementNode] = Field(default_factory=list)
