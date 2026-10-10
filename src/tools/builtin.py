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
    if scope.filesystem_prefixes:
        resolved_str = str(resolved).lower()
        allowed = False
        for prefix in scope.filesystem_prefixes:
            p_res = str(Path(prefix).resolve()).lower()
            if resolved_str == p_res or resolved_str.startswith(p_res + os.sep):
                allowed = True
                break
        if not allowed:
            p_list = sorted(scope.filesystem_prefixes)
            raise PermissionError(f"Path '{resolved}' is outside allowed scope prefixes: {p_list}")
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


def file_search_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    """Search for a text pattern in files within a directory."""
    path_str = str(args.get("directory", "."))
    pattern = str(args.get("pattern", ""))
    ext_filter = args.get("extension")
    path = _validate_safe_path(Path(path_str), scope)
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path_str}")

    matches = []
    for root, _, files in os.walk(path):
        for f in sorted(files):
            if ext_filter and not f.endswith(str(ext_filter)):
                continue
            file_p = Path(root) / f
            try:
                text = file_p.read_text(encoding="utf-8", errors="ignore")
                for line_idx, line in enumerate(text.splitlines(), start=1):
                    if pattern.lower() in line.lower():
                        rel = os.path.relpath(file_p, path)
                        matches.append(f"{rel}:{line_idx}: {line.strip()[:100]}")
                        if len(matches) >= 50:
                            break
            except Exception:
                continue
        if len(matches) >= 50:
            break

    return "\n".join(matches) if matches else "[NO_MATCHES_FOUND]"


def dev_runner_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    """Execute controlled project development verification in a subprocess."""
    v_type = str(args.get("verification_type", "syntax"))
    target = str(args.get("target", "."))
    code_snippet = args.get("code")

    if v_type == "syntax":
        if code_snippet:
            code = f"compile({repr(str(code_snippet))}, '<snippet>', 'exec'); print('SYNTAX_VALID')"
        else:
            code = (
                "import py_compile, sys; "
                f"sys.exit(0 if py_compile.compile(r'{target}', doraise=False) else 1)"
            )
        cmd = [sys.executable, "-c", code]
    elif v_type == "test":
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", target, "-p", "*test*.py"]
    elif v_type == "lint":
        code = (
            "import ast; "
            f"ast.parse(open({repr(target)}, encoding='utf-8').read()); "
            "print('AST_SYNTAX_OK')"
        )
        cmd = [sys.executable, "-c", code]
    else:
        raise ValueError(f"Unsupported verification_type: {v_type}")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    res = proc.stdout.strip()
    if proc.stderr:
        res += f"\n[stderr]\n{proc.stderr.strip()}"
    if proc.returncode != 0:
        res += f"\n[exit_code: {proc.returncode}]"
    return res or "[VERIFICATION_SUCCESS]"


def research_query_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    """Query technical reference documentation via research providers."""
    import json

    from src.manager.research import (
        CompositeResearchProvider,
        ErrorClass,
        LocalDocResearchProvider,
        OnlineDocResearchProvider,
    )

    query = str(args["query"])
    err_cls_str = str(args.get("error_class", "UNKNOWN"))
    net_permitted = bool(args.get("network_permitted", False))

    err_cls = getattr(ErrorClass, err_cls_str, ErrorClass.UNKNOWN)
    local = LocalDocResearchProvider()
    online = OnlineDocResearchProvider(network_permitted=net_permitted) if net_permitted else None
    provider = CompositeResearchProvider(
        local_provider=local, online_provider=online, network_permitted=net_permitted
    )

    rec = provider.research(query=query, error_class=err_cls)
    if not rec:
        return "[NO_RESEARCH_FINDINGS]"

    return json.dumps(
        {
            "findings_summary": rec.findings_summary,
            "provider_type": rec.provider_type,
            "sources": [s.model_dump() for s in rec.sources],
            "suggested_adjustments": rec.suggested_adjustments,
        }
    )


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

FILE_SEARCH_TOOL = ToolDefinition(
    name="file_search",
    description="Search text pattern across files in a directory.",
    action_name="filesystem.read",
    risk=ActionRisk.LOW,
    scope=ResourceScope(filesystem_prefixes=frozenset(["."])),
    parameters=(
        ToolParameterSchema(
            name="directory",
            param_type=ParamType.PATH,
            required=False,
            description="Directory path to search.",
        ),
        ToolParameterSchema(
            name="pattern",
            param_type=ParamType.STRING,
            required=True,
            description="Text pattern to search for.",
        ),
        ToolParameterSchema(
            name="extension",
            param_type=ParamType.STRING,
            required=False,
            description="Optional file extension filter (e.g. '.py').",
        ),
    ),
)

DEV_RUNNER_TOOL = ToolDefinition(
    name="dev_runner",
    description="Execute controlled project development verification (syntax, test, lint).",
    action_name="process.run_test",
    risk=ActionRisk.MEDIUM,
    scope=ResourceScope(
        allowed_process_commands=frozenset([sys.executable, "python", "python.exe"])
    ),
    parameters=(
        ToolParameterSchema(
            name="verification_type",
            param_type=ParamType.STRING,
            required=True,
            description="Verification type ('syntax', 'test', 'lint').",
        ),
        ToolParameterSchema(
            name="target",
            param_type=ParamType.STRING,
            required=False,
            description="Target file or directory path.",
        ),
        ToolParameterSchema(
            name="code",
            param_type=ParamType.STRING,
            required=False,
            description="Optional inline code string to evaluate.",
        ),
    ),
)

RESEARCH_QUERY_TOOL = ToolDefinition(
    name="research_query",
    description="Query local or permitted online developer documentation for error research.",
    action_name="filesystem.read",
    risk=ActionRisk.LOW,
    scope=ResourceScope(filesystem_prefixes=frozenset(["."])),
    parameters=(
        ToolParameterSchema(
            name="query",
            param_type=ParamType.STRING,
            required=True,
            description="Technical error search query.",
        ),
        ToolParameterSchema(
            name="error_class",
            param_type=ParamType.STRING,
            required=False,
            description="Categorized error class name.",
        ),
        ToolParameterSchema(
            name="network_permitted",
            param_type=ParamType.BOOLEAN,
            required=False,
            description="Whether network egress is permitted for online research.",
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
    search_tool = FILE_SEARCH_TOOL
    research_tool = RESEARCH_QUERY_TOOL

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
        search_tool = ToolDefinition(
            name=FILE_SEARCH_TOOL.name,
            description=FILE_SEARCH_TOOL.description,
            action_name=FILE_SEARCH_TOOL.action_name,
            risk=FILE_SEARCH_TOOL.risk,
            scope=scope,
            parameters=FILE_SEARCH_TOOL.parameters,
        )
        research_tool = ToolDefinition(
            name=RESEARCH_QUERY_TOOL.name,
            description=RESEARCH_QUERY_TOOL.description,
            action_name=RESEARCH_QUERY_TOOL.action_name,
            risk=RESEARCH_QUERY_TOOL.risk,
            scope=scope,
            parameters=RESEARCH_QUERY_TOOL.parameters,
        )

    broker.register(write_tool, inline_handler=file_write_handler)
    broker.register(read_tool, inline_handler=file_read_handler)
    broker.register(list_tool, inline_handler=file_list_handler)
    broker.register(PYTHON_EVAL_TOOL, inline_handler=python_eval_handler)
    broker.register(search_tool, inline_handler=file_search_handler)
    broker.register(DEV_RUNNER_TOOL, inline_handler=dev_runner_handler)
    broker.register(research_tool, inline_handler=research_query_handler)
