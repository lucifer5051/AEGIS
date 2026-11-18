"""SAGE Windows Desktop Automation Subsystem -- MCP Stdio Client & Process Manager.

Implements the Model Context Protocol (MCP) JSON-RPC 2.0 transport over standard I/O pipes.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.desktop.config import WindowsMCPConfig
from src.desktop.errors import (
    DesktopProcessError,
    DesktopSecurityError,
    DesktopTimeoutError,
    ToolManifestValidationError,
)
from src.desktop.policy import ToolPolicyMetadata, WindowsDesktopToolPolicy

logger = logging.getLogger(__name__)


class WindowsMCPClient:
    """Manages an external windows-mcp-server process over stdio JSON-RPC 2.0."""

    def __init__(
        self,
        config: WindowsMCPConfig,
        policy: Optional[WindowsDesktopToolPolicy] = None,
        process_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config = config
        self._policy = policy or WindowsDesktopToolPolicy(config)
        self._process_factory = process_factory or subprocess.Popen
        self._process: Optional[Any] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._is_running: bool = False
        self._req_id: int = 1
        self._lock = threading.Lock()
        self._restart_count: int = 0
        self._authorized_tools: Dict[str, Dict[str, Any]] = {}
        self._pending_responses: Dict[int, Any] = {}
        self._reader_thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._is_running and self._process is not None and self._process.poll() is None

    @property
    def authorized_tools(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._authorized_tools)

    def start(self) -> None:
        """Launch the external Windows MCP server process and complete MCP handshake."""
        if not self._config.enabled:
            raise DesktopSecurityError(
                "Cannot start Windows MCP server: integration is disabled in configuration."
            )

        if not self._config.executable_path:
            raise DesktopSecurityError(
                "Cannot start Windows MCP server: executable_path is not configured."
            )

        exe_path = Path(self._config.executable_path)
        # Note: In mock/test environments, process_factory may mock execution;
        # otherwise verify existence on real filesystem
        if self._process_factory == subprocess.Popen and not exe_path.exists():
            raise DesktopProcessError(
                f"Configured Windows MCP executable does not exist: '{exe_path}'"
            )

        self._spawn_process()
        self._perform_handshake()

    def _spawn_process(self) -> None:
        """Spawn the external MCP subprocess."""
        cmd = [str(self._config.executable_path)]
        if self._config.persona:
            cmd.extend(["--persona", self._config.persona])

        try:
            self._process = self._process_factory(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._is_running = True
        except Exception as exc:
            raise DesktopProcessError(
                f"Failed to spawn Windows MCP server process: {exc}"
            ) from exc

        # Drain stderr in separate thread to prevent pipe deadlocks
        def _drain_stderr():
            try:
                for line in iter(self._process.stderr.readline, ""):
                    if line:
                        logger.debug("[WindowsMCP stderr] %s", line.strip())
            except Exception:
                pass

        self._stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _send_json_rpc(self, method: str, params: Optional[Dict[str, Any]] = None, is_notification: bool = False) -> Optional[int]:
        """Send a JSON-RPC 2.0 message over stdin."""
        if not self.is_running:
            raise DesktopProcessError("Cannot send message: MCP server process is not running.")

        req_id = None
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not is_notification:
            with self._lock:
                req_id = self._req_id
                self._req_id += 1
            msg["id"] = req_id

        if params is not None:
            msg["params"] = params

        line = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(line)
            self._process.stdin.flush()
        except Exception as exc:
            self._is_running = False
            raise DesktopProcessError(f"Failed writing to MCP stdin pipe: {exc}") from exc

        return req_id

    def _read_json_rpc_response(self, expected_id: Optional[int], timeout: float) -> Dict[str, Any]:
        """Read a JSON-RPC response from stdout with a timeout."""
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if not self.is_running:
                raise DesktopProcessError("MCP process terminated unexpectedly while awaiting response.")

            # Read line from stdout
            line = self._process.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue

            try:
                data = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            if not isinstance(data, dict):
                continue

            # Check if this matches expected request id
            if expected_id is not None and data.get("id") == expected_id:
                if "error" in data:
                    raise DesktopProcessError(
                        f"MCP server returned error for request {expected_id}: {data['error']}"
                    )
                return data

        raise DesktopTimeoutError(f"Timed out after {timeout:.1f}s waiting for MCP response.")

    def _perform_handshake(self) -> None:
        """Execute MCP initialization handshake and tools discovery."""
        timeout = self._config.startup_timeout_seconds

        # Step 1: initialize
        init_id = self._send_json_rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "SAGE", "version": "1.0"},
            },
        )
        init_resp = self._read_json_rpc_response(init_id, timeout)
        logger.info("MCP initialize handshake successful: %s", init_resp.get("result", {}).get("serverInfo"))

        # Step 2: notifications/initialized
        self._send_json_rpc("notifications/initialized", is_notification=True)

        # Step 3: tools/list
        list_id = self._send_json_rpc("tools/list", {})
        list_resp = self._read_json_rpc_response(list_id, timeout)

        raw_tools = list_resp.get("result", {}).get("tools", [])
        validated_tools = self._policy.validate_tool_manifest(raw_tools)

        self._authorized_tools = {
            t["name"]: t for t in validated_tools
        }
        logger.info(
            "Discovered and authorized %d Windows MCP tools (filtered from %d advertised)",
            len(self._authorized_tools),
            len(raw_tools),
        )

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call an authorized tool through the MCP protocol."""
        if not self.is_running:
            # Handle bounded restart
            if self._restart_count < self._config.max_restart_attempts:
                self._restart_count += 1
                logger.warning(
                    "Windows MCP server dead; attempting restart %d/%d",
                    self._restart_count,
                    self._config.max_restart_attempts,
                )
                self.start()
            else:
                raise DesktopProcessError(
                    f"Windows MCP server process crashed and exceeded max restarts ({self._config.max_restart_attempts})."
                )

        # Tool authorization check
        canonical = self._policy.resolve_canonical_tool(tool_name)
        if canonical is None:
            raise DesktopSecurityError(f"Tool '{tool_name}' is not recognized or authorized.")

        # Application allowlist check for app launch
        if canonical.sage_name == "windows.launch_app":
            app_target = arguments.get("app_name") or arguments.get("application") or arguments.get("path", "")
            validated_app = self._policy.validate_application_launch(str(app_target))
            arguments = dict(arguments)
            arguments["app_name"] = validated_app

        # Find matching tool name on upstream server
        upstream_name = tool_name
        for auth_name, tool_def in self._authorized_tools.items():
            if auth_name == tool_name or self._policy.resolve_canonical_tool(auth_name) == canonical:
                upstream_name = auth_name
                break

        req_id = self._send_json_rpc(
            "tools/call",
            {"name": upstream_name, "arguments": arguments},
        )
        resp = self._read_json_rpc_response(req_id, self._config.request_timeout_seconds)

        result_content = resp.get("result", {})
        # Tag and sanitize output
        if isinstance(result_content, dict) and "content" in result_content:
            text = str(result_content["content"])
            return self._policy.sanitize_untrusted_output(text)
        elif isinstance(result_content, str):
            return self._policy.sanitize_untrusted_output(result_content)

        return {"result": result_content, "untrusted_data": True}

    def stop(self) -> None:
        """Gracefully shut down the external MCP server process."""
        self._is_running = False
        if self._process is not None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
                self._process.wait(timeout=2.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
