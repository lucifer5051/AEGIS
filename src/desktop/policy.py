"""SAGE Windows Desktop Automation Subsystem -- Tool Policy & Boundary Enforcer."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.desktop.config import WindowsMCPConfig
from src.desktop.errors import DesktopSecurityError, ToolManifestValidationError
from src.models.errors import redact_secrets_from_text
from src.trust.audit import _check_no_secrets
from src.trust.capability import ActionRisk

logger = logging.getLogger(__name__)

# Dangerous shell characters that indicate command injection attempts
_DANGEROUS_COMMAND_CHARS = re.compile(r"[\&\|\;\>\<\`\$\(\)\{\}\n\r\x00]")

@dataclass(frozen=True)
class ToolPolicyMetadata:
    sage_name: str
    action_name: str
    risk: ActionRisk
    description: str


# Canonical mapping from supported upstream tool names to SAGE tool models
CANONICAL_TOOL_MAP: Dict[str, ToolPolicyMetadata] = {
    "snapshot": ToolPolicyMetadata(
        sage_name="windows.snapshot",
        action_name="desktop.inspect",
        risk=ActionRisk.LOW,
        description="Capture hierarchical accessibility UI automation tree.",
    ),
    "screenshot": ToolPolicyMetadata(
        sage_name="windows.screenshot",
        action_name="desktop.inspect",
        risk=ActionRisk.LOW,
        description="Capture visual screenshot of desktop or targeted window.",
    ),
    "displayinventory": ToolPolicyMetadata(
        sage_name="windows.display_inventory",
        action_name="desktop.inspect",
        risk=ActionRisk.LOW,
        description="Enumerate connected displays, resolutions, and active windows.",
    ),
    "gettext": ToolPolicyMetadata(
        sage_name="windows.get_text",
        action_name="desktop.read",
        risk=ActionRisk.LOW,
        description="Extract text content from UI automation element.",
    ),
    "wait": ToolPolicyMetadata(
        sage_name="windows.wait",
        action_name="desktop.inspect",
        risk=ActionRisk.LOW,
        description="Bounded pause for UI synchronization.",
    ),
    "waitfor": ToolPolicyMetadata(
        sage_name="windows.wait_for",
        action_name="desktop.inspect",
        risk=ActionRisk.LOW,
        description="Wait for a specific UI element or state to appear.",
    ),
    "invoke": ToolPolicyMetadata(
        sage_name="windows.invoke",
        action_name="desktop.interact",
        risk=ActionRisk.MEDIUM,
        description="Execute a UI Automation invoke pattern on a target element.",
    ),
    "click": ToolPolicyMetadata(
        sage_name="windows.click",
        action_name="desktop.interact",
        risk=ActionRisk.MEDIUM,
        description="Simulate mouse click on specified element or coordinate.",
    ),
    "type": ToolPolicyMetadata(
        sage_name="windows.type",
        action_name="desktop.interact",
        risk=ActionRisk.MEDIUM,
        description="Type text into active or targeted UI element.",
    ),
    "scroll": ToolPolicyMetadata(
        sage_name="windows.scroll",
        action_name="desktop.interact",
        risk=ActionRisk.MEDIUM,
        description="Scroll UI container horizontally or vertically.",
    ),
    "move": ToolPolicyMetadata(
        sage_name="windows.move",
        action_name="desktop.interact",
        risk=ActionRisk.MEDIUM,
        description="Move cursor to target element coordinates.",
    ),
    "shortcut": ToolPolicyMetadata(
        sage_name="windows.shortcut",
        action_name="desktop.interact",
        risk=ActionRisk.MEDIUM,
        description="Send a keyboard shortcut key combination.",
    ),
    "launchapp": ToolPolicyMetadata(
        sage_name="windows.launch_app",
        action_name="desktop.launch",
        risk=ActionRisk.HIGH,
        description="Launch an explicitly allowlisted desktop application.",
    ),
}

# Aliases for upstream naming variations (e.g. ui_click -> click)
TOOL_NAME_ALIASES: Dict[str, str] = {
    "ui_snapshot": "snapshot",
    "screen_capture": "screenshot",
    "capture_screenshot": "screenshot",
    "display_inventory": "displayinventory",
    "ui_get_text": "gettext",
    "ui_wait": "wait",
    "ui_wait_for": "waitfor",
    "ui_invoke": "invoke",
    "ui_click": "click",
    "ui_type": "type",
    "ui_scroll": "scroll",
    "ui_move": "move",
    "ui_shortcut": "shortcut",
    "app_launch": "launchapp",
    "launch_application": "launchapp",
}


class WindowsDesktopToolPolicy:
    """Enforces SAGE policy invariants, application boundaries, and manifest validation."""

    def __init__(self, config: WindowsMCPConfig) -> None:
        self._config = config
        self._allowed_tools_norm: Set[str] = {
            self._normalize_name(t) for t in config.allowed_tools
        }
        self._denied_tools_norm: Set[str] = {
            self._normalize_name(t) for t in config.denied_tools
        }
        self._allowed_apps_norm: Set[str] = {
            app.strip().lower() for app in config.allowed_applications if app.strip()
        }

    @staticmethod
    def _normalize_name(name: str) -> str:
        clean = name.strip().lower().replace("_", "").replace("-", "")
        return clean

    def resolve_canonical_tool(self, raw_tool_name: str) -> Optional[ToolPolicyMetadata]:
        """Resolve an upstream tool name to its canonical SAGE ToolPolicyMetadata."""
        clean = raw_tool_name.strip().lower()
        if clean.startswith("windows."):
            clean = clean[len("windows."):]
        if clean in TOOL_NAME_ALIASES:
            clean = TOOL_NAME_ALIASES[clean]
        norm = self._normalize_name(clean)
        return CANONICAL_TOOL_MAP.get(norm)

    def validate_tool_manifest(self, advertised_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validate an upstream server's advertised tools manifest.

        Rejects forbidden destructive tools and returns only authorized tools.
        Raises ToolManifestValidationError if any critical security policy is violated.
        """
        authorized_tools: List[Dict[str, Any]] = []

        for tool in advertised_tools:
            name = str(tool.get("name", "")).strip()
            if not name:
                continue

            norm_name = self._normalize_name(name)

            # Check explicit denied tools
            for denied in self._denied_tools_norm:
                if denied in norm_name or norm_name in denied:
                    logger.warning(
                        "Excluded forbidden upstream tool '%s' matching denied pattern '%s'",
                        name,
                        denied,
                    )
                    break
            else:
                # Not denied: check if in allowed tools and in CANONICAL_TOOL_MAP
                canonical = self.resolve_canonical_tool(name)
                if canonical is None:
                    logger.info("Ignoring unmapped/unknown upstream tool '%s'", name)
                    continue

                if norm_name not in self._allowed_tools_norm and self._normalize_name(canonical.sage_name.split(".")[-1]) not in self._allowed_tools_norm:
                    logger.info("Tool '%s' not present in allowed_tools list; skipping", name)
                    continue

                authorized_tools.append(tool)

        return authorized_tools

    def validate_application_launch(self, app_path_or_name: str) -> str:
        """Enforce strict application allowlist.

        Prevents arbitrary executable launching, path traversal, and shell injection.
        """
        if not app_path_or_name or not app_path_or_name.strip():
            raise DesktopSecurityError("Application name cannot be empty.")

        cleaned = app_path_or_name.strip()

        # Reject command injection characters
        if _DANGEROUS_COMMAND_CHARS.search(cleaned):
            raise DesktopSecurityError(
                f"Application target contains forbidden shell control characters: '{cleaned}'"
            )

        # Reject path traversal
        if ".." in cleaned:
            raise DesktopSecurityError(
                f"Application target contains path traversal sequence '..': '{cleaned}'"
            )

        # Extract base executable name
        base_name = Path(cleaned).name.lower()

        if base_name not in self._allowed_apps_norm:
            raise DesktopSecurityError(
                f"Application '{base_name}' is not in the permitted application allowlist: "
                f"{sorted(list(self._allowed_apps_norm))}"
            )

        return base_name

    def sanitize_untrusted_output(self, raw_output: str) -> Dict[str, Any]:
        """Tag and sanitize output returned from desktop inspection.

        Treats screen text, accessibility trees, and OCR as untrusted user input
        to prevent indirect prompt injection from influencing authorization decisions.
        """
        redacted = redact_secrets_from_text(raw_output)
        return {
            "content": redacted,
            "untrusted_data": True,
            "source": "windows_desktop",
        }
