"""SAGE Tool Broker -- Public API Surface.

The Tool Broker is the single gateway for all tool invocations in SAGE.

Quick start:

    from src.trust import build_trust_authority
    from src.tools import ToolBroker, ToolDefinition, ToolParameterSchema, ParamType
    from src.trust.capability import ActionRisk, ResourceScope
    import sqlite3

    conn = sqlite3.connect(":memory:")  # use real DB in production
    authority = build_trust_authority(conn)

    broker = ToolBroker(authority)
    broker.register(
        ToolDefinition(
            name="fs.read_file",
            action_name="filesystem.read",
            risk=ActionRisk.LOW,
            description="Read a file and return its contents.",
            parameters=[
                ToolParameterSchema(name="path", param_type=ParamType.PATH),
            ],
            scope=ResourceScope(
                filesystem_prefixes=frozenset({"/home/user/project"}),
            ),
        ),
        inline_handler=lambda args, scope: open(args["path"]).read(),
    )

    result = broker.invoke("fs.read_file", args={"path": "/home/user/project/foo.py"}, actor="user")

All other submodules can be imported directly if lower-level types are needed.
"""

from src.tools.broker import ToolBroker
from src.tools.builtin import (
    FILE_LIST_TOOL,
    FILE_READ_TOOL,
    FILE_WRITE_TOOL,
    PYTHON_EVAL_TOOL,
    register_builtin_tools,
)
from src.tools.errors import (
    DuplicateToolError,
    OutputSanitizationError,
    ToolArgumentBoundsError,
    ToolArgumentError,
    ToolArgumentMissingError,
    ToolArgumentTypeError,
    ToolAuthorizationError,
    ToolBrokerError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolSandboxError,
    ToolTimeoutError,
)
from src.tools.models import (
    ParamType,
    ToolDefinition,
    ToolParameterSchema,
    ToolResult,
    ToolResultStatus,
)
from src.tools.sanitizer import sanitize_output

__all__ = [
    # Broker
    "ToolBroker",
    "register_builtin_tools",
    "FILE_WRITE_TOOL",
    "FILE_READ_TOOL",
    "FILE_LIST_TOOL",
    "PYTHON_EVAL_TOOL",
    # Models
    "ParamType",
    "ToolDefinition",
    "ToolParameterSchema",
    "ToolResult",
    "ToolResultStatus",
    # Sanitizer
    "sanitize_output",
    # Errors
    "ToolBrokerError",
    "ToolRegistrationError",
    "ToolNotFoundError",
    "DuplicateToolError",
    "ToolArgumentError",
    "ToolArgumentTypeError",
    "ToolArgumentMissingError",
    "ToolArgumentBoundsError",
    "ToolExecutionError",
    "ToolTimeoutError",
    "ToolSandboxError",
    "ToolAuthorizationError",
    "OutputSanitizationError",
]
