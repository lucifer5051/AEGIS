"""SAGE Windows Desktop Automation Subsystem -- Tool Broker Integration.

Registers restricted Windows MCP tools with SAGE's central ToolBroker and TrustCore.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.desktop.errors import DesktopSecurityError
from src.desktop.mcp_client import WindowsMCPClient
from src.desktop.policy import CANONICAL_TOOL_MAP, WindowsDesktopToolPolicy
from src.tools.broker import ToolBroker
from src.tools.models import ParamType, ToolDefinition, ToolParameterSchema
from src.trust.capability import ActionRisk, ResourceScope

logger = logging.getLogger(__name__)


def build_tool_parameters(sage_name: str) -> List[ToolParameterSchema]:
    """Define deterministic parameter schemas for desktop automation tools."""
    short_name = sage_name.split(".")[-1]

    if short_name == "snapshot":
        return [
            ToolParameterSchema(
                name="target_window",
                param_type=ParamType.STRING,
                required=False,
                description="Optional window title or accessibility name to scope tree.",
            )
        ]
    elif short_name == "screenshot":
        return [
            ToolParameterSchema(
                name="target_window",
                param_type=ParamType.STRING,
                required=False,
                description="Optional window name or monitor index.",
            )
        ]
    elif short_name == "display_inventory":
        return []
    elif short_name == "get_text":
        return [
            ToolParameterSchema(
                name="element_id",
                param_type=ParamType.STRING,
                required=True,
                description="Automation ID, Name, or locator of the UI element.",
            )
        ]
    elif short_name == "wait":
        return [
            ToolParameterSchema(
                name="duration_ms",
                param_type=ParamType.INTEGER,
                required=False,
                min_value=10,
                max_value=10000,
                description="Pause duration in milliseconds.",
            )
        ]
    elif short_name == "wait_for":
        return [
            ToolParameterSchema(
                name="element_id",
                param_type=ParamType.STRING,
                required=True,
                description="Automation ID or element name to wait for.",
            ),
            ToolParameterSchema(
                name="timeout_ms",
                param_type=ParamType.INTEGER,
                required=False,
                min_value=100,
                max_value=30000,
                description="Timeout in milliseconds.",
            ),
        ]
    elif short_name == "invoke":
        return [
            ToolParameterSchema(
                name="element_id",
                param_type=ParamType.STRING,
                required=True,
                description="Target UI element automation ID or selector to invoke.",
            )
        ]
    elif short_name == "click":
        return [
            ToolParameterSchema(
                name="element_id",
                param_type=ParamType.STRING,
                required=False,
                description="Target element ID (preferred over coordinates).",
            ),
            ToolParameterSchema(
                name="x",
                param_type=ParamType.INTEGER,
                required=False,
                description="X screen coordinate fallback.",
            ),
            ToolParameterSchema(
                name="y",
                param_type=ParamType.INTEGER,
                required=False,
                description="Y screen coordinate fallback.",
            ),
        ]
    elif short_name == "type":
        return [
            ToolParameterSchema(
                name="text",
                param_type=ParamType.STRING,
                required=True,
                description="Text to type into active/target element.",
            ),
            ToolParameterSchema(
                name="element_id",
                param_type=ParamType.STRING,
                required=False,
                description="Target element automation ID.",
            ),
        ]
    elif short_name == "scroll":
        return [
            ToolParameterSchema(
                name="direction",
                param_type=ParamType.STRING,
                required=False,
                description="Scroll direction: 'up', 'down', 'left', 'right'.",
            ),
            ToolParameterSchema(
                name="amount",
                param_type=ParamType.INTEGER,
                required=False,
                description="Number of scroll steps.",
            ),
        ]
    elif short_name == "move":
        return [
            ToolParameterSchema(
                name="x",
                param_type=ParamType.INTEGER,
                required=True,
                description="Target X coordinate.",
            ),
            ToolParameterSchema(
                name="y",
                param_type=ParamType.INTEGER,
                required=True,
                description="Target Y coordinate.",
            ),
        ]
    elif short_name == "shortcut":
        return [
            ToolParameterSchema(
                name="keys",
                param_type=ParamType.STRING,
                required=True,
                description="Key combination string, e.g. 'ctrl+s', 'alt+f4'.",
            )
        ]
    elif short_name == "launch_app":
        return [
            ToolParameterSchema(
                name="app_name",
                param_type=ParamType.STRING,
                required=True,
                description="Allowlisted application executable (e.g. 'notepad.exe').",
            )
        ]

    return []


def register_desktop_tools(
    broker: ToolBroker,
    client: WindowsMCPClient,
    policy: WindowsDesktopToolPolicy,
) -> List[ToolDefinition]:
    """Register authorized Windows desktop automation tools into the SAGE ToolBroker.

    Returns the list of registered ToolDefinition models.
    """
    registered_defs: List[ToolDefinition] = []

    for upstream_name, tool_info in client.authorized_tools.items():
        canonical = policy.resolve_canonical_tool(upstream_name)
        if canonical is None:
            continue

        # Prevent duplicate registrations
        if canonical.sage_name in broker._tools:
            continue

        params = build_tool_parameters(canonical.sage_name)

        tool_def = ToolDefinition(
            name=canonical.sage_name,
            action_name=canonical.action_name,
            risk=canonical.risk,
            description=canonical.description,
            parameters=params,
            scope=ResourceScope(),
            timeout_seconds=30.0,
        )

        def make_handler(target_tool: str):
            def handler(args: Dict[str, Any], scope: ResourceScope) -> str:
                res = client.call_tool(target_tool, args)
                if isinstance(res, dict):
                    return json.dumps(res)
                return str(res)
            return handler

        broker.register(tool_def, inline_handler=make_handler(canonical.sage_name))
        registered_defs.append(tool_def)
        logger.info("Registered desktop tool '%s' with ToolBroker", tool_def.name)

    return registered_defs


def register_desktop_action_tools(
    broker: ToolBroker,
    adapter: Any,
    policy: WindowsDesktopToolPolicy,
) -> List[ToolDefinition]:
    """Register desktop action adapter tools with ToolBroker."""
    registered_defs: List[ToolDefinition] = []

    for name, canonical in CANONICAL_TOOL_MAP.items():
        if canonical.sage_name in broker._tools:
            continue

        params = build_tool_parameters(canonical.sage_name)
        tool_def = ToolDefinition(
            name=canonical.sage_name,
            action_name=canonical.action_name,
            risk=canonical.risk,
            description=canonical.description,
            parameters=params,
            scope=ResourceScope(),
            timeout_seconds=30.0,
        )

        def make_handler(short: str):
            def handler(args: Dict[str, Any], scope: ResourceScope) -> str:
                if short == "snapshot":
                    res = adapter.snapshot(hwnd=int(args.get("target_window", 0) or 0))
                elif short == "screenshot":
                    res = adapter.screenshot(hwnd=int(args.get("target_window", 0) or 0))
                elif short == "get_text":
                    res = adapter.get_text(automation_id=str(args.get("element_id", "")))
                elif short == "invoke":
                    res = adapter.invoke(automation_id=str(args.get("element_id", "")))
                elif short == "click":
                    res = adapter.click(
                        x=int(args.get("x", -1) or -1),
                        y=int(args.get("y", -1) or -1),
                        automation_id=str(args.get("element_id", "")),
                    )
                elif short == "type":
                    res = adapter.type_text(text=str(args.get("text", "")))
                elif short == "shortcut":
                    res = adapter.shortcut(keys=str(args.get("keys", "")))
                elif short == "launch_app":
                    res = adapter.launch_app(app_name=str(args.get("app_name", "")))
                elif short in ("wait", "wait_for"):
                    import time
                    time.sleep(float(args.get("duration_ms", 500) or 500) / 1000.0)
                    res = {"status": "SUCCESS", "waited": True}
                else:
                    res = {"status": "SUCCESS"}

                if isinstance(res, dict):
                    return json.dumps(res)
                return str(res)
            return handler

        short_tool = canonical.sage_name.split(".")[-1]
        broker.register(tool_def, inline_handler=make_handler(short_tool))
        registered_defs.append(tool_def)
        logger.info("Registered desktop action tool '%s' with ToolBroker", tool_def.name)

    return registered_defs
