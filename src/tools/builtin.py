"""SAGE Built-in Tools -- Safe Local Execution Primitives.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § Tool Broker

Registers safe default tools for local file operations and code execution.
All tools require explicit capability tokens from TrustAuthority and are
sanitized upon completion.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from src.storage.paths import is_path_inside_recovery_repo
from src.tools.broker import ToolBroker
from src.tools.models import (
    ParamType,
    ToolDefinition,
    ToolParameterSchema,
)
from src.trust.capability import ActionRisk, ResourceScope


def _validate_safe_path(target_path: Path, scope: ResourceScope) -> Path:
    """Ensure path does not violate safety boundaries."""
    resolved = target_path.resolve()
    if is_path_inside_recovery_repo(resolved):
        raise PermissionError(f"Modifying recovery directory '{resolved}' is forbidden.")
    # Prevent tampering with git internal object database
    for part in resolved.parts:
        if part.lower() == ".git":
            raise PermissionError(f"Accessing git internal directory '{resolved}' is forbidden.")
    return resolved


def file_write_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    """Write text content to a local file."""
    path_str = str(args["path"])
    content = str(args["content"])
    path = _validate_safe_path(Path(path_str), scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    byte_count = os.path.getsize(path)
    return f"File written successfully: {path.name} ({byte_count} bytes)"


def file_read_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    """Read text content from a local file."""
    path_str = str(args["path"])
    path = _validate_safe_path(Path(path_str), scope)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path_str}")
    content = path.read_text(encoding="utf-8")
    return content


def file_list_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    """List files in a directory."""
    path_str = str(args.get("directory", "."))
    path = _validate_safe_path(Path(path_str), scope)
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path_str}")
    items = sorted(os.listdir(path))
    return "\n".join(items) if items else "[EMPTY_DIRECTORY]"


def python_eval_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    """Execute Python code in a safe subprocess."""
    code = str(args["code"])
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    output = proc.stdout
    if proc.stderr:
        output += f"\n[stderr]\n{proc.stderr}"
    if proc.returncode != 0:
        output += f"\n[exit_code: {proc.returncode}]"
    return output.strip() or "[NO_OUTPUT]"


FILE_WRITE_TOOL = ToolDefinition(
    name="file_write",
    description="Write UTF-8 text content to a local file path.",
    action_name="filesystem.write",
    risk=ActionRisk.MEDIUM,
    scope=ResourceScope(filesystem_prefixes=frozenset(["."])),
    parameters=(
        ToolParameterSchema(
            name="path",
            param_type=ParamType.PATH,
            required=True,
            description="Relative or absolute file path to write to.",
        ),
        ToolParameterSchema(
            name="content",
            param_type=ParamType.STRING,
            required=True,
            description="Text content to write into the file.",
        ),
    ),
)

FILE_READ_TOOL = ToolDefinition(
    name="file_read",
    description="Read UTF-8 text content from a local file path.",
    action_name="filesystem.read",
    risk=ActionRisk.LOW,
    scope=ResourceScope(filesystem_prefixes=frozenset(["."])),
    parameters=(
        ToolParameterSchema(
            name="path",
            param_type=ParamType.PATH,
            required=True,
            description="Path of file to read.",
        ),
    ),
)

FILE_LIST_TOOL = ToolDefinition(
    name="file_list",
    description="List contents of a directory.",
    action_name="filesystem.list",
    risk=ActionRisk.LOW,
    scope=ResourceScope(filesystem_prefixes=frozenset(["."])),
    parameters=(
        ToolParameterSchema(
            name="directory",
            param_type=ParamType.PATH,
            required=False,
            description="Directory path to inspect (defaults to current working directory).",
        ),
    ),
)

PYTHON_EVAL_TOOL = ToolDefinition(
    name="python_eval",
    description="Execute a Python code string in an isolated subprocess.",
    action_name="process.run_test",
    risk=ActionRisk.MEDIUM,
    scope=ResourceScope(
        allowed_process_commands=frozenset([sys.executable, "python", "python.exe"])
    ),
    parameters=(
        ToolParameterSchema(
            name="code",
            param_type=ParamType.STRING,
            required=True,
            description="Python source code to execute.",
        ),
    ),
)


def register_builtin_tools(
    broker: ToolBroker,
    allowed_roots: Optional[Sequence[Path]] = None,
) -> None:
    """Register the canonical suite of built-in safe local tools into the broker."""
    # Scope adjustment if specific roots provided
    write_tool = FILE_WRITE_TOOL
    read_tool = FILE_READ_TOOL
    list_tool = FILE_LIST_TOOL

    if allowed_roots:
        root_strs = frozenset([str(r.resolve()) for r in allowed_roots])
        scope = ResourceScope(filesystem_prefixes=root_strs)
        write_tool = ToolDefinition(
            name=FILE_WRITE_TOOL.name,
            description=FILE_WRITE_TOOL.description,
            action_name=FILE_WRITE_TOOL.action_name,
            risk=FILE_WRITE_TOOL.risk,
            scope=scope,
            parameters=FILE_WRITE_TOOL.parameters,
        )
        read_tool = ToolDefinition(
            name=FILE_READ_TOOL.name,
            description=FILE_READ_TOOL.description,
            action_name=FILE_READ_TOOL.action_name,
            risk=FILE_READ_TOOL.risk,
            scope=scope,
            parameters=FILE_READ_TOOL.parameters,
        )
        list_tool = ToolDefinition(
            name=FILE_LIST_TOOL.name,
            description=FILE_LIST_TOOL.description,
            action_name=FILE_LIST_TOOL.action_name,
            risk=FILE_LIST_TOOL.risk,
            scope=scope,
            parameters=FILE_LIST_TOOL.parameters,
        )

    broker.register(write_tool, inline_handler=file_write_handler)
    broker.register(read_tool, inline_handler=file_read_handler)
    broker.register(list_tool, inline_handler=file_list_handler)
    broker.register(PYTHON_EVAL_TOOL, inline_handler=python_eval_handler)
