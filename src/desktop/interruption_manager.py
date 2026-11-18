"""SAGE Desktop Awareness Subsystem -- Interruption Manager.

Detects, classifies, and handles unexpected desktop events disrupting ongoing tasks.
"""

from __future__ import annotations

import logging
from enum import Enum, unique
from typing import NamedTuple, Optional

from src.desktop.events import DesktopEvent, DesktopEventType

logger = logging.getLogger(__name__)


@unique
class InterruptionSeverity(str, Enum):
    """Categorization of desktop interruptions."""

    NONE = "NONE"
    TRANSIENT = "TRANSIENT"  # Non-modal notification, background toast
    MODAL = "MODAL"          # Dialog or prompt within or blocking the target app
    CRITICAL = "CRITICAL"    # UAC elevation, system security dialog, application crash


class InterruptionDecision(NamedTuple):
    should_pause: bool
    severity: InterruptionSeverity
    reason: str


KNOWN_SECURITY_PROCESSES = {
    "consent.exe",
    "credentialuibroker.exe",
    "securityhealthsystray.exe",
}

KNOWN_MODAL_TITLES = {
    "save as",
    "open file",
    "confirm",
    "warning",
    "error",
    "user account control",
    "windows security",
}


class InterruptionManager:
    """Evaluates incoming desktop events to detect task-disrupting interruptions."""

    def evaluate_interruption(
        self,
        event: DesktopEvent,
        target_process: Optional[str] = None,
        target_hwnd: Optional[int] = None,
    ) -> InterruptionDecision:
        """Determine whether an event constitutes an interruption to the current active task."""
        proc_lower = (event.process_name or "").lower()
        title_lower = (event.window_title or "").lower()

        # 1. Critical System / Security Interruption (UAC or Credential prompt)
        if proc_lower in KNOWN_SECURITY_PROCESSES or "user account control" in title_lower:
            return InterruptionDecision(
                should_pause=True,
                severity=InterruptionSeverity.CRITICAL,
                reason="System elevation or UAC security prompt appeared.",
            )

        # 2. Foreground switch away from target application
        if event.event_type == DesktopEventType.FOREGROUND_CHANGED.value:
            if target_process and proc_lower and proc_lower != target_process.lower():
                # Focus shifted to a different application
                return InterruptionDecision(
                    should_pause=True,
                    severity=InterruptionSeverity.MODAL,
                    reason=f"Active focus shifted to unexpected process '{proc_lower}' (expected '{target_process}').",
                )

            if target_hwnd and event.window_handle > 0 and event.window_handle != target_hwnd:
                # Same process, but different window opened (e.g. modal dialog)
                for modal_word in KNOWN_MODAL_TITLES:
                    if modal_word in title_lower:
                        return InterruptionDecision(
                            should_pause=True,
                            severity=InterruptionSeverity.MODAL,
                            reason=f"Modal dialog '{event.window_title}' opened in target application.",
                        )

        # 3. Window Destruction of Target
        if event.event_type == DesktopEventType.WINDOW_CLOSED.value:
            if target_hwnd and event.window_handle == target_hwnd:
                return InterruptionDecision(
                    should_pause=True,
                    severity=InterruptionSeverity.CRITICAL,
                    reason=f"Target window HWND={target_hwnd} was closed or terminated.",
                )

        return InterruptionDecision(
            should_pause=False,
            severity=InterruptionSeverity.NONE,
            reason="Normal operating state.",
        )
