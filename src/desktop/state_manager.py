"""SAGE Desktop Awareness Subsystem -- State Manager.

Maintains the authoritative in-memory representation of Windows desktop state.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from src.desktop.events import DesktopEvent, DesktopEventType, EventBounds

logger = logging.getLogger(__name__)


@dataclass
class WindowState:
    hwnd: int
    pid: int
    process_name: str
    title: str
    bounds: Optional[EventBounds] = None
    is_active: bool = False
    last_updated: float = field(default_factory=time.time)


@dataclass
class ElementState:
    element_id: Optional[str]
    control_type: Optional[str]
    runtime_id: Optional[List[int]]
    name: str
    bounds: Optional[EventBounds] = None
    hwnd: int = 0
    pid: int = 0
    last_updated: float = field(default_factory=time.time)


class DesktopStateManager:
    """Manages incremental desktop state, event coalescing, and consistency reconciliation."""

    def __init__(self, coalescing_window_seconds: float = 0.1) -> None:
        self._coalescing_window = coalescing_window_seconds
        self._lock = threading.Lock()

        self._active_window: Optional[WindowState] = None
        self._open_windows: Dict[int, WindowState] = {}
        self._focused_element: Optional[ElementState] = None
        self._event_history: Deque[DesktopEvent] = collections.deque(maxlen=100)
        self._subscribers: List[Callable[[DesktopEvent, Optional[WindowState]], None]] = []

    @property
    def active_window(self) -> Optional[WindowState]:
        with self._lock:
            return self._active_window

    @property
    def open_windows(self) -> Dict[int, WindowState]:
        with self._lock:
            return dict(self._open_windows)

    @property
    def focused_element(self) -> Optional[ElementState]:
        with self._lock:
            return self._focused_element

    @property
    def event_history(self) -> List[DesktopEvent]:
        with self._lock:
            return list(self._event_history)

    def subscribe(self, callback: Callable[[DesktopEvent, Optional[WindowState]], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def handle_event(self, event: DesktopEvent) -> None:
        """Process an incoming desktop event, apply state updates, and debounce bursts."""
        with self._lock:
            # Check for event coalescing (same event category on same window within coalescing window)
            if self._event_history:
                last_evt = self._event_history[-1]
                if (
                    last_evt.event_type == event.event_type
                    and last_evt.window_handle == event.window_handle
                    and (event.timestamp - last_evt.timestamp) < self._coalescing_window
                ):
                    # Coalesce: update timestamp and metadata on the last event
                    self._event_history[-1] = event
                    return

            self._event_history.append(event)
            prev_active = self._active_window

            # Process state transitions
            if event.event_type == DesktopEventType.FOREGROUND_CHANGED.value:
                hwnd = event.window_handle
                if hwnd > 0:
                    win_state = self._open_windows.get(hwnd)
                    if not win_state:
                        win_state = WindowState(
                            hwnd=hwnd,
                            pid=event.process_id,
                            process_name=event.process_name,
                            title=event.window_title,
                            bounds=event.bounds,
                            is_active=True,
                            last_updated=event.timestamp,
                        )
                        self._open_windows[hwnd] = win_state
                    else:
                        win_state.is_active = True
                        win_state.title = event.window_title or win_state.title
                        win_state.process_name = event.process_name or win_state.process_name
                        win_state.bounds = event.bounds or win_state.bounds
                        win_state.last_updated = event.timestamp

                    # Mark old active inactive
                    if self._active_window and self._active_window.hwnd != hwnd:
                        self._active_window.is_active = False

                    self._active_window = win_state

            elif event.event_type == DesktopEventType.WINDOW_OPENED.value:
                hwnd = event.window_handle
                if hwnd > 0 and hwnd not in self._open_windows:
                    self._open_windows[hwnd] = WindowState(
                        hwnd=hwnd,
                        pid=event.process_id,
                        process_name=event.process_name,
                        title=event.window_title,
                        bounds=event.bounds,
                        is_active=False,
                        last_updated=event.timestamp,
                    )

            elif event.event_type == DesktopEventType.WINDOW_CLOSED.value:
                hwnd = event.window_handle
                self._open_windows.pop(hwnd, None)
                if self._active_window and self._active_window.hwnd == hwnd:
                    self._active_window = None

            elif event.event_type == DesktopEventType.FOCUS_CHANGED.value:
                self._focused_element = ElementState(
                    element_id=event.element_id,
                    control_type=event.control_type,
                    runtime_id=event.runtime_id,
                    name=event.window_title,
                    bounds=event.bounds,
                    hwnd=event.window_handle,
                    pid=event.process_id,
                    last_updated=event.timestamp,
                )

            subs = list(self._subscribers)

        # Notify subscribers outside lock
        for cb in subs:
            try:
                cb(event, prev_active)
            except Exception as exc:
                logger.error("State manager subscriber error: %s", exc)

    def reconcile_state(self, live_active_hwnd: int, live_title: str, live_pid: int, live_proc: str) -> None:
        """Low-frequency reconciliation to resynchronize internal model if drift occurred."""
        with self._lock:
            if live_active_hwnd > 0:
                win = self._open_windows.get(live_active_hwnd)
                if not win:
                    win = WindowState(
                        hwnd=live_active_hwnd,
                        pid=live_pid,
                        process_name=live_proc,
                        title=live_title,
                        is_active=True,
                    )
                    self._open_windows[live_active_hwnd] = win
                else:
                    win.title = live_title
                    win.is_active = True

                self._active_window = win
