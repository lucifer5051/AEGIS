"""SAGE Desktop Awareness Subsystem -- Semantic Action Adapter.

Translates authorized SAGE tool invocations into concrete UI Automation actions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.desktop.client import DesktopObserverClient
from src.desktop.element_registry import DesktopElementRegistry
from src.desktop.errors import DesktopProcessError, DesktopSecurityError
from src.desktop.policy import WindowsDesktopToolPolicy

logger = logging.getLogger(__name__)


class DesktopActionAdapter:
    """Dispatches semantic UIA actions to the observer and enforces perimeter boundaries."""

    def __init__(
        self,
        client: DesktopObserverClient,
        policy: WindowsDesktopToolPolicy,
        registry: Optional[DesktopElementRegistry] = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._registry = registry or DesktopElementRegistry()

    def snapshot(self, hwnd: int = 0, max_depth: int = 5) -> Dict[str, Any]:
        """Capture hierarchical accessibility tree."""
        raw = self._client.inspect(hwnd=hwnd, max_depth=max_depth)
        return self._policy.sanitize_untrusted_output(str(raw))

    def screenshot(self, hwnd: int = 0) -> Dict[str, Any]:
        """Capture desktop or window visual frame."""
        raw = self._client.screenshot(hwnd=hwnd)
        return {
            "screenshot_base64": raw.get("screenshot_base64", ""),
            "bounds": raw.get("bounds", {}),
            "untrusted_data": True,
        }

    def get_text(self, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        """Read text from an element via inspect tree."""
        raw = self._client.inspect(hwnd=0, max_depth=3)
        return self._policy.sanitize_untrusted_output(str(raw))

    def invoke(self, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        """Trigger an InvokePattern on a control."""
        if automation_id and self._registry.get_element(automation_id) is not None:
            is_stale, reason = self._registry.is_reference_stale(
                automation_id,
                current_session_generation=getattr(self._client, "session_generation", 1),
            )
            if is_stale:
                raise DesktopSecurityError(f"Stale element reference rejected: {reason}")

        res = self._client.invoke(automation_id=automation_id, name=name)
        return {"status": "SUCCESS", "invoked": True, "detail": res}

    def set_value(self, value: str, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        """Set text into an editable control via ValuePattern."""
        if automation_id and self._registry.get_element(automation_id) is not None:
            is_stale, reason = self._registry.is_reference_stale(
                automation_id,
                current_session_generation=getattr(self._client, "session_generation", 1),
            )
            if is_stale:
                raise DesktopSecurityError(f"Stale element reference rejected: {reason}")

        res = self._client.set_value(value=value, automation_id=automation_id, name=name)
        return {"status": "SUCCESS", "value_set": True, "detail": res}

    def click(self, x: int = -1, y: int = -1, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        """Click a control by selector or coordinate fallback."""
        if automation_id and self._registry.get_element(automation_id) is not None:
            is_stale, reason = self._registry.is_reference_stale(
                automation_id,
                current_session_generation=getattr(self._client, "session_generation", 1),
            )
            if is_stale:
                raise DesktopSecurityError(f"Stale element reference rejected: {reason}")

        res = self._client.click(x=x, y=y, automation_id=automation_id, name=name)
        return {"status": "SUCCESS", "clicked": True, "detail": res}

    def type_text(self, text: str) -> Dict[str, Any]:
        """Simulate keyboard typing into active element."""
        res = self._client.type_text(text=text)
        return {"status": "SUCCESS", "typed": True, "detail": res}

    def shortcut(self, keys: str) -> Dict[str, Any]:
        """Send a keyboard shortcut."""
        res = self._client.shortcut(keys=keys)
        return {"status": "SUCCESS", "shortcut_sent": True, "detail": res}

    def launch_app(self, app_name: str) -> Dict[str, Any]:
        """Launch an allowlisted application."""
        validated_app = self._policy.validate_application_launch(app_name)
        # Execute allowlisted application
        import subprocess
        proc = subprocess.Popen([validated_app])
        return {"status": "SUCCESS", "launched": validated_app, "pid": proc.pid}
