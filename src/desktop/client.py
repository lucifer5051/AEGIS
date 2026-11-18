"""SAGE Desktop Awareness Subsystem -- Observer IPC Client & Process Manager.

Manages the persistent sage-desktop-watcher native process over standard I/O pipes.
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
)
from src.desktop.events import DesktopEvent

logger = logging.getLogger(__name__)

DEFAULT_WATCHER_EXE_PATH = (
    Path(__file__).parent.parent.parent
    / "tools"
    / "sage-desktop-watcher"
    / "bin"
    / "Release"
    / "net9.0-windows"
    / "sage-desktop-watcher.exe"
)


class DesktopObserverClient:
    """Manages the lifecycle, asynchronous event stream, and commands of sage-desktop-watcher."""

    def __init__(
        self,
        executable_path: Optional[str] = None,
        process_factory: Optional[Callable[..., Any]] = None,
        startup_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 15.0,
        max_restarts: int = 3,
    ) -> None:
        self._executable_path = executable_path or str(DEFAULT_WATCHER_EXE_PATH)
        self._process_factory = process_factory or subprocess.Popen
        self._startup_timeout = startup_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._max_restarts = max_restarts

        self._process: Optional[Any] = None
        self._is_running: bool = False
        self._session_generation: int = 1
        self._restart_count: int = 0
        self._req_id: int = 1
        self._lock = threading.Lock()

        self._event_listeners: List[Callable[[DesktopEvent], None]] = []
        self._pending_commands: Dict[int, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._is_running and self._process is not None and self._process.poll() is None

    @property
    def session_generation(self) -> int:
        return self._session_generation

    def add_event_listener(self, listener: Callable[[DesktopEvent], None]) -> None:
        """Register a callback invoked whenever a desktop event arrives."""
        with self._lock:
            self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[DesktopEvent], None]) -> None:
        """Unregister an event listener."""
        with self._lock:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

    def start(self) -> None:
        """Start the native observer process and await the OBSERVER_READY handshake."""
        if self.is_running:
            logger.info("Desktop observer is already running.")
            return

        exe = Path(self._executable_path)
        if self._process_factory == subprocess.Popen and not exe.exists():
            raise DesktopProcessError(f"Native desktop watcher binary not found at '{exe}'")

        try:
            self._process = self._process_factory(
                [str(self._executable_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._is_running = True
        except Exception as exc:
            raise DesktopProcessError(f"Failed to start desktop watcher process: {exc}") from exc

        # Start stdout reader thread
        self._reader_thread = threading.Thread(target=self._stdout_loop, daemon=True, name="DesktopWatcherReader")
        self._reader_thread.start()

        # Start stderr drain thread
        def _drain_stderr():
            try:
                for line in iter(self._process.stderr.readline, ""):
                    if line:
                        logger.debug("[desktop-watcher stderr] %s", line.strip())
            except Exception:
                pass

        self._stderr_thread = threading.Thread(target=_drain_stderr, daemon=True, name="DesktopWatcherStderr")
        self._stderr_thread.start()

        # Ping to verify handshake
        try:
            res = self.ping(timeout=self._startup_timeout)
            logger.info("Desktop observer started successfully: %s", res)
        except Exception as exc:
            self.stop()
            raise DesktopProcessError(f"Desktop observer handshake failed: {exc}") from exc

    def _stdout_loop(self) -> None:
        """Read and dispatch events or command responses from the observer stdout pipe."""
        while self._is_running and self._process is not None:
            try:
                line = self._process.stdout.readline()
                if not line:
                    if self._process.poll() is not None:
                        break
                    time.sleep(0.02)
                    continue

                line_str = line.strip()
                if not line_str:
                    continue

                data = json.loads(line_str)
                if not isinstance(data, dict):
                    continue

                # Check if it is a command response
                if "Id" in data or "id" in data:
                    req_id = data.get("Id", data.get("id"))
                    with self._lock:
                        if req_id in self._pending_commands:
                            evt, container = self._pending_commands[req_id]
                            container.update(data)
                            evt.set()
                            continue

                # Otherwise check if it is a desktop event
                evt_type = data.get("EventType") or data.get("event_type")
                if evt_type:
                    event = self._normalize_event(data)
                    # Notify listeners
                    with self._lock:
                        listeners = list(self._event_listeners)
                    for listener in listeners:
                        try:
                            listener(event)
                        except Exception as exc:
                            logger.error("Error in desktop event listener: %s", exc)

            except Exception as exc:
                if not self._is_running:
                    break
                logger.debug("Error processing desktop watcher output: %s", exc)

        self._is_running = False

    def _normalize_event(self, data: Dict[str, Any]) -> DesktopEvent:
        """Normalize JSON payload from C# observer into DesktopEvent model."""
        return DesktopEvent(
            version=str(data.get("Version", data.get("version", "1.0"))),
            event_id=str(data.get("EventId", data.get("event_id", ""))),
            event_type=str(data.get("EventType", data.get("event_type", "GENERIC_WIN_EVENT"))),
            timestamp=float(data.get("Timestamp", data.get("timestamp", time.time()))),
            process_id=int(data.get("ProcessId", data.get("process_id", 0))),
            window_handle=int(data.get("WindowHandle", data.get("window_handle", 0))),
            session_generation=int(data.get("SessionGeneration", data.get("session_generation", self._session_generation))),
            process_name=str(data.get("ProcessName", data.get("process_name", ""))),
            window_title=str(data.get("WindowTitle", data.get("window_title", ""))),
            element_id=data.get("ElementId", data.get("element_id")),
            control_type=data.get("ControlType", data.get("control_type")),
            runtime_id=data.get("RuntimeId", data.get("runtime_id")),
            bounds=data.get("Bounds", data.get("bounds")),
        )

    def send_command(self, command: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Dispatch a synchronous command to the observer over stdin and await response."""
        if not self.is_running:
            if self._restart_count < self._max_restarts:
                self._restart_count += 1
                self._session_generation += 1
                logger.warning("Observer process dead; restarting (attempt %d/%d)", self._restart_count, self._max_restarts)
                self.start()
            else:
                raise DesktopProcessError("Desktop observer is not running and exceeded max restarts.")

        with self._lock:
            req_id = self._req_id
            self._req_id += 1
            cmd_payload: Dict[str, Any] = {"command": command, "id": req_id}
            if params:
                cmd_payload.update(params)

            done_event = threading.Event()
            result_container: Dict[str, Any] = {}
            self._pending_commands[req_id] = (done_event, result_container)

        cmd_json = json.dumps(cmd_payload) + "\n"
        try:
            self._process.stdin.write(cmd_json)
            self._process.stdin.flush()
        except Exception as exc:
            self._is_running = False
            with self._lock:
                self._pending_commands.pop(req_id, None)
            raise DesktopProcessError(f"Failed writing command to observer stdin: {exc}") from exc

        to = timeout or self._request_timeout
        if not done_event.wait(timeout=to):
            with self._lock:
                self._pending_commands.pop(req_id, None)
            raise DesktopTimeoutError(f"Timed out after {to:.1f}s awaiting observer response for '{command}'.")

        with self._lock:
            self._pending_commands.pop(req_id, None)

        status = result_container.get("Status", result_container.get("status", "SUCCESS"))
        if status == "ERROR":
            err_msg = result_container.get("Error", result_container.get("error", "Unknown error in observer"))
            raise DesktopProcessError(f"Observer command '{command}' failed: {err_msg}")

        return result_container.get("Result", result_container.get("result", result_container))

    def ping(self, timeout: float = 3.0) -> Dict[str, Any]:
        return self.send_command("ping", timeout=timeout)

    def inspect(self, hwnd: int = 0, max_depth: int = 5) -> Dict[str, Any]:
        return self.send_command("inspect", {"hwnd": hwnd, "max_depth": max_depth})

    def invoke(self, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        return self.send_command("invoke", {"automation_id": automation_id, "name": name})

    def set_value(self, value: str, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        return self.send_command("set_value", {"automation_id": automation_id, "name": name, "value": value})

    def click(self, x: int = -1, y: int = -1, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        return self.send_command("click", {"x": x, "y": y, "automation_id": automation_id, "name": name})

    def type_text(self, text: str) -> Dict[str, Any]:
        return self.send_command("type", {"text": text})

    def shortcut(self, keys: str) -> Dict[str, Any]:
        return self.send_command("shortcut", {"keys": keys})

    def screenshot(self, hwnd: int = 0) -> Dict[str, Any]:
        return self.send_command("screenshot", {"hwnd": hwnd})

    def stop(self) -> None:
        """Gracefully terminate observer process."""
        self._is_running = False
        if self._process is not None:
            try:
                self._process.stdin.write('{"command":"shutdown"}\n')
                self._process.stdin.flush()
                self._process.wait(timeout=1.5)
            except Exception:
                try:
                    self._process.terminate()
                except Exception:
                    pass
            self._process = None
