"""SAGE Desktop Awareness Subsystem -- Differential State Tracker.

Computes semantic and structural differentials between desktop states to prevent context bloat.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.desktop.events import DesktopEvent, DesktopEventType
from src.desktop.state_manager import WindowState

logger = logging.getLogger(__name__)


class DifferentialStateTracker:
    """Tracks state transitions and computes concise textual differentials."""

    def __init__(self) -> None:
        self._last_active_hwnd: Optional[int] = None
        self._last_active_title: Optional[str] = None
        self._last_active_proc: Optional[str] = None

    def compute_differential(
        self,
        event: DesktopEvent,
        prev_active: Optional[WindowState],
    ) -> Optional[str]:
        """Compute human/model-readable differential summary for an event."""
        diff_str: Optional[str] = None

        if event.event_type == DesktopEventType.FOREGROUND_CHANGED.value:
            curr_title = event.window_title or "(Untitled)"
            curr_proc = event.process_name or "Unknown"

            if prev_active and prev_active.hwnd != event.window_handle:
                diff_str = (
                    f"[DESKTOP_DIFF: FOREGROUND_CHANGED] Focus shifted from "
                    f"'{prev_active.process_name}' ('{prev_active.title}') to "
                    f"'{curr_proc}' ('{curr_title}', HWND={event.window_handle})"
                )
            elif not prev_active:
                diff_str = (
                    f"[DESKTOP_DIFF: FOREGROUND_ACQUIRED] Foreground set to "
                    f"'{curr_proc}' ('{curr_title}', HWND={event.window_handle})"
                )

            self._last_active_hwnd = event.window_handle
            self._last_active_title = curr_title
            self._last_active_proc = curr_proc

        elif event.event_type == DesktopEventType.WINDOW_OPENED.value:
            diff_str = (
                f"[DESKTOP_DIFF: WINDOW_OPENED] New window appeared: "
                f"'{event.process_name}' ('{event.window_title}', HWND={event.window_handle})"
            )

        elif event.event_type == DesktopEventType.WINDOW_CLOSED.value:
            diff_str = (
                f"[DESKTOP_DIFF: WINDOW_CLOSED] Window destroyed: "
                f"'{event.process_name}' ('{event.window_title}', HWND={event.window_handle})"
            )

        elif event.event_type == DesktopEventType.FOCUS_CHANGED.value:
            if event.element_id or event.control_type:
                diff_str = (
                    f"[DESKTOP_DIFF: FOCUS_MOVED] Focus on {event.control_type or 'Element'} "
                    f"'{event.element_id or event.window_title}' in '{event.process_name}'"
                )

        return diff_str
