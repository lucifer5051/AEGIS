"""SAGE Remote Control Gateway -- Supervised Baileys Node.js Sidecar Transport.

Canonical Reference:
    AEGIS Prompt 036 (Phase 6: Strict Receive-Only WhatsApp Integration)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md § 3, § 7
    src/gateway/sidecar_protocol.py

Invariants:
- Strictly receive-only: outbound sends and automatic replies are explicitly prohibited.
- Supervised child process over stdin/stdout using newline-delimited JSON envelopes.
- Stderr is strictly reserved for diagnostic logs (never mixed with JSON-lines protocol).
- Zero network ports are exposed.
- Session credentials and private keys are never logged or exposed in envelopes.
- Safely handles malformed frames, process disconnects, reconnects, and shutdown.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Any, Dict, List, Optional, Union

from src.gateway.errors import TransportError
from src.gateway.models import OutboundMessage
from src.gateway.sidecar_protocol import (
    SidecarEnvelope,
    SidecarEventType,
    SidecarProtocolError,
    parse_sidecar_line,
    serialize_sidecar_envelope,
)
from src.gateway.whatsapp_transport import WhatsAppTransport
from src.trust.approval import ApprovalStore

logger = logging.getLogger(__name__)


class BaileysSidecarSupervisor(WhatsAppTransport):
    """Supervises a Node.js Baileys sidecar process over stdin/stdout IPC in receive-only mode."""

    def __init__(
        self,
        node_script_path: Optional[str] = None,
        auth_dir: Optional[str] = None,
        auto_start: bool = False,
    ) -> None:
        self._node_script_path = node_script_path
        self._auth_dir = auth_dir
        self._process: Optional[subprocess.Popen[str]] = None
        self._inbound_queue: List[Dict[str, Any]] = []
        self._queue_lock = threading.Lock()
        self._connected: bool = False
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        if auto_start and node_script_path and os.path.exists(node_script_path):
            self.start()

    @property
    def is_connected(self) -> bool:
        """Whether the sidecar process is active and the connection is established."""
        if self._process is None or self._process.poll() is not None:
            return False
        return self._connected

    def start(self, cmd: Optional[List[str]] = None) -> None:
        """Launch the supervised Node.js sidecar child process."""
        if self._process is not None and self._process.poll() is None:
            logger.warning("Sidecar process is already running.")
            return

        command = cmd
        if command is None:
            if not self._node_script_path or not os.path.exists(self._node_script_path):
                raise TransportError(f"Sidecar script not found at '{self._node_script_path}'")
            command = ["node", self._node_script_path]
            if self._auth_dir:
                command.extend(["--auth-dir", self._auth_dir])

        self._stop_event.clear()
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line-buffered
                encoding="utf-8",
            )
            self._connected = True
        except Exception as exc:
            self._connected = False
            raise TransportError(f"Failed to spawn sidecar process: {exc}") from exc

        # Spawn stdout reader thread
        self._reader_thread = threading.Thread(
            target=self._stdout_reader_loop, daemon=True, name="BaileysSidecarStdoutReader"
        )
        self._reader_thread.start()

        # Spawn stderr diagnostic logging thread
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop, daemon=True, name="BaileysSidecarStderrReader"
        )
        self._stderr_thread.start()
        logger.info("Baileys sidecar supervisor started (PID=%s)", self._process.pid)

    def stop(self, timeout_seconds: float = 3.0) -> None:
        """Gracefully terminate the sidecar child process."""
        self._stop_event.set()
        self._connected = False

        if self._process is None:
            return

        # Attempt graceful shutdown envelope via stdin
        if self._process.stdin and not self._process.stdin.closed:
            try:
                envelope = SidecarEnvelope(
                    type=SidecarEventType.SHUTDOWN,
                    payload={"reason": "supervisor_stopping"},
                )
                self._process.stdin.write(serialize_sidecar_envelope(envelope))
                self._process.stdin.flush()
            except Exception:
                pass

        try:
            self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Sidecar process did not exit gracefully; terminating PID=%s", self._process.pid
            )
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()

        self._process = None
        logger.info("Baileys sidecar supervisor stopped.")

    def _stdout_reader_loop(self) -> None:
        """Continuously read and validate JSON-line envelopes from child process stdout."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return

        for line in iter(proc.stdout.readline, ""):
            if self._stop_event.is_set():
                break
            line_str = line.strip()
            if not line_str:
                continue

            try:
                envelope = parse_sidecar_line(line_str)
            except SidecarProtocolError as exc:
                logger.error("Sidecar protocol error on stdout: %s", exc)
                continue

            if envelope.type == SidecarEventType.CONNECTION_UPDATE:
                status = envelope.payload.get("status", "")
                self._connected = status in ("open", "connected")
                logger.info("Sidecar connection status updated: %s", status)
            elif envelope.type == SidecarEventType.INBOUND_MESSAGE:
                with self._queue_lock:
                    self._inbound_queue.append(envelope.payload)
            elif envelope.type == SidecarEventType.ERROR:
                err_msg = envelope.payload.get("error", "Unknown sidecar error")
                logger.warning("Sidecar reported error: %s", err_msg)
            elif envelope.type == SidecarEventType.HEARTBEAT:
                pass  # Keepalive

    def _stderr_reader_loop(self) -> None:
        """Stream diagnostic logs from child process stderr to logger."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return

        for line in iter(proc.stderr.readline, ""):
            if self._stop_event.is_set():
                break
            diagnostic = line.strip()
            if diagnostic:
                # Diagnostics never contain secrets per audit
                logger.debug("[BaileysSidecar STDERR] %s", diagnostic)

    def inject_event(self, event_payload: Dict[str, Any]) -> None:
        """Inject an event directly into the inbound queue (offline/testing)."""
        with self._queue_lock:
            self._inbound_queue.append(event_payload)

    def get_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve and drain up to limit events from the inbound queue."""
        with self._queue_lock:
            if not self._inbound_queue:
                return []
            events = self._inbound_queue[:limit]
            self._inbound_queue = self._inbound_queue[limit:]
            return events

    def send_message(
        self,
        recipient_jid: Optional[Union[str, OutboundMessage]] = None,
        text: Optional[str] = None,
        correlation_id: Optional[str] = None,
        approval_id: Optional[str] = None,
        approval_store: Optional[ApprovalStore] = None,
        require_approval: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Strict Receive-Only Boundary: Outbound sending is explicitly prohibited."""
        logger.error(
            "Attempted outbound message dispatch in receive-only mode to recipient=%s",
            recipient_jid,
        )
        raise TransportError(
            "Outbound WhatsApp sending is strictly disabled in receive-only mode. "
            "Autonomous outbound messaging is prohibited."
        )
