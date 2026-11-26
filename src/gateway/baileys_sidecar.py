"""SAGE Remote Control Gateway -- Supervised Baileys Node.js Sidecar Transport.

Canonical Reference:
    AEGIS Prompt 036 (Phase 6: Strict Receive-Only WhatsApp Integration)
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md § 3, § 7
    src/gateway/sidecar_protocol.py

Invariants:
- Guarded outbound sending: requires explicit cryptographic SendAuthorization token.
- Autonomous outbound messaging without authorization is strictly prohibited.
- Supervised child process over stdin/stdout using newline-delimited JSON envelopes.
- Stderr is strictly reserved for diagnostic logs (never mixed with JSON-lines protocol).
- Zero network ports are exposed.
- Session credentials and private keys are never logged or exposed in envelopes.
- Safely handles malformed frames, process disconnects, reconnects, and shutdown.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import uuid4

from src.gateway.contact_alias import CANONICAL_PN_REGEX
from src.gateway.echo_registry import SelfEchoRegistry
from src.gateway.errors import ChannelConfigError as ConfigurationError, GatewayError, TransportError
from src.gateway.models import OutboundMessage
from src.gateway.send_authorization import (
    AuthorizationMismatchError,
    RateLimitedError,
    SendAuthorization,
    SidecarTimeoutError,
    TargetNotAllowedError,
)
from src.gateway.sidecar_protocol import (
    SidecarEnvelope,
    SidecarEventType,
    SidecarProtocolError,
    parse_sidecar_line,
    serialize_sidecar_envelope,
)
from src.gateway.whatsapp_transport import WhatsAppTransport
from src.trust.approval import ApprovalStore
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def generate_allowlist_file(
    allowed_jids: List[str],
    owner_jid: Optional[str] = None,
    output_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Generate the bridge's allowlist JSON array from whitelisted canonical PN JIDs.

    Invariants:
    - Atomically written via temporary file and os.replace.
    - Rejects any non-canonical entry (raises ValueError).
    - Excludes the owner's JID.
    """
    cleaned_jids: List[str] = []
    for jid in allowed_jids:
        if not jid or not isinstance(jid, str):
            raise ValueError(f"Invalid allowlist entry: {jid!r}")
        jid_str = jid.strip()
        if not CANONICAL_PN_REGEX.match(jid_str):
            raise ValueError(
                f"Non-canonical WhatsApp JID '{jid_str}' rejected from allowlist. "
                "Expected format: <digits>@s.whatsapp.net"
            )
        if owner_jid and jid_str == owner_jid.strip():
            continue  # Never include owner in bridge allowlist
        cleaned_jids.append(jid_str)

    target_path = Path(output_path) if output_path else Path.home() / ".aegis" / "whatsapp_allowlist.json"
    target_path.parent.mkdir(parents=True, exist_ok=True)

    temp_fd, temp_file_path = tempfile.mkstemp(
        dir=str(target_path.parent), prefix="allowlist_tmp_", suffix=".json"
    )
    with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
        json.dump(cleaned_jids, f, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp_file_path, target_path)
    logger.info("Atomically generated WhatsApp bridge allowlist at %s (%d entries)", target_path, len(cleaned_jids))
    return target_path


class BaileysSidecarSupervisor(WhatsAppTransport):
    """Supervises a Node.js Baileys sidecar process over stdin/stdout IPC."""

    def __init__(
        self,
        node_script_path: Optional[str] = None,
        auth_dir: Optional[str] = None,
        auto_start: bool = False,
        allowlist_path: Optional[Union[str, Path]] = None,
        allowed_jids: Optional[List[str]] = None,
        owner_jid: Optional[str] = None,
        echo_registry: Optional[SelfEchoRegistry] = None,
    ) -> None:
        self._node_script_path = node_script_path
        self._auth_dir = auth_dir
        self._allowed_jids = list(allowed_jids) if allowed_jids else []
        self._owner_jid = owner_jid
        self._echo_registry = echo_registry
        if allowlist_path is not None:
            self._allowlist_path: Optional[Path] = Path(allowlist_path)
        elif allowed_jids:
            self._allowlist_path = generate_allowlist_file(allowed_jids, owner_jid)
        else:
            self._allowlist_path = None

        self._process: Optional[subprocess.Popen[str]] = None
        self._inbound_queue: List[Dict[str, Any]] = []
        self._queue_lock = threading.Lock()
        self._connected: bool = False
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._pending_sends: Dict[str, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()

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
            if self._allowlist_path and os.path.exists(self._allowlist_path):
                command.extend(["--allowlist", str(self._allowlist_path)])

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
            elif envelope.type == SidecarEventType.OUTBOUND_ACK:
                cid = envelope.correlation_id
                with self._pending_lock:
                    if cid in self._pending_sends:
                        evt, res_box = self._pending_sends[cid]
                        res_box["type"] = "ack"
                        res_box["payload"] = envelope.payload
                        evt.set()
            elif envelope.type == SidecarEventType.ERROR:
                err_msg = envelope.payload.get("error", "Unknown sidecar error")
                logger.warning("Sidecar reported error: %s", err_msg)
                cid = envelope.correlation_id
                with self._pending_lock:
                    if cid in self._pending_sends:
                        evt, res_box = self._pending_sends[cid]
                        res_box["type"] = "error"
                        res_box["payload"] = envelope.payload
                        evt.set()
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
        *,
        authorization: Optional[SendAuthorization] = None,
        timeout: float = 30.0,
        correlation_id: Optional[str] = None,
        approval_id: Optional[str] = None,
        approval_store: Optional[ApprovalStore] = None,
        require_approval: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Guarded Outbound Send Path.

        Requires explicit SendAuthorization token. Autonomous or unapproved sending is prohibited.
        """
        if authorization is None:
            logger.error(
                "Attempted outbound message dispatch without SendAuthorization to recipient=%s",
                recipient_jid,
            )
            raise TransportError(
                "Outbound WhatsApp sending is strictly disabled in receive-only mode. "
                "Autonomous outbound messaging is prohibited."
            )

        if self._process is None or self._process.poll() is not None:
            raise TransportError("Sidecar process is not running.")

        if isinstance(recipient_jid, OutboundMessage):
            text = text or recipient_jid.content
            recipient_jid = recipient_jid.recipient_id

        recipient_str = str(recipient_jid or "").strip()
        if not recipient_str:
            raise ValueError("Recipient JID cannot be empty.")

        if recipient_str != authorization.recipient_jid:
            raise AuthorizationMismatchError(
                f"Recipient JID '{recipient_str}' does not match authorization target '{authorization.recipient_jid}'"
            )

        text_str = str(text or "").strip()
        if not text_str:
            raise ValueError("Outbound message text cannot be empty.")
        if len(text_str) > 4000:
            raise ValueError("Outbound message text exceeds 4000 characters limit.")

        _check_no_secrets(text_str)
        text_sha = hashlib.sha256(text_str.encode("utf-8")).hexdigest()
        if text_sha != authorization.text_sha256:
            raise AuthorizationMismatchError(
                f"Outbound text SHA-256 digest '{text_sha}' does not match authorization '{authorization.text_sha256}'"
            )

        is_owner = bool(self._owner_jid and recipient_str == self._owner_jid.strip())
        is_whitelisted = recipient_str in self._allowed_jids
        if not (CANONICAL_PN_REGEX.match(recipient_str) and (is_whitelisted or is_owner)):
            raise TargetNotAllowedError(
                f"Recipient '{recipient_str}' is not in the authorized whitelist or owner JID."
            )

        cid = correlation_id or str(uuid4())
        evt = threading.Event()
        res_box: Dict[str, Any] = {}
        with self._pending_lock:
            self._pending_sends[cid] = (evt, res_box)

        if self._process.stdin is None or self._process.stdin.closed:
            with self._pending_lock:
                self._pending_sends.pop(cid, None)
            raise TransportError("Sidecar stdin is closed or unavailable.")

        envelope = SidecarEnvelope(
            type=SidecarEventType.OUTBOUND_SEND,
            correlation_id=cid,
            payload={"to": recipient_str, "text": text_str},
        )
        try:
            self._process.stdin.write(serialize_sidecar_envelope(envelope))
            self._process.stdin.flush()
        except Exception as exc:
            with self._pending_lock:
                self._pending_sends.pop(cid, None)
            raise TransportError(f"Failed to write outbound envelope to sidecar stdin: {exc}") from exc

        signaled = evt.wait(timeout=timeout)
        with self._pending_lock:
            self._pending_sends.pop(cid, None)

        if not signaled:
            raise SidecarTimeoutError(f"Timed out waiting for send ack for correlation_id={cid}")

        resp_type = res_box.get("type")
        payload = res_box.get("payload", {})
        if resp_type == "error":
            err_code = payload.get("error_code") or payload.get("code") or payload.get("error", "")
            if err_code == "TARGET_NOT_ALLOWED":
                raise TargetNotAllowedError(f"Bridge rejected target: {payload.get('error', err_code)}")
            elif err_code == "RATE_LIMITED":
                raise RateLimitedError(f"Bridge rate limited send: {payload.get('error', err_code)}")
            else:
                raise GatewayError(f"Bridge reported error: {payload.get('error', err_code)}")

        msg_id = payload.get("id") or payload.get("message_id")
        if msg_id and self._echo_registry is not None:
            self._echo_registry.register(str(msg_id))

        return payload

    def send_owner_notice(
        self,
        text: str,
        correlation_id: Optional[str] = None,
        timeout: float = 30.0,
    ) -> Any:
        """Send an administrative notice to the owner's self-chat without requiring SendAuthorization.

        Targets strictly self._owner_jid. Validates length (<=4000) and ensures no secrets leak.
        Registers returned message ID with self._echo_registry to prevent processing self-echoes.
        """
        if not self._owner_jid or not self._owner_jid.strip():
            raise ConfigurationError("Owner JID is not configured; cannot send owner notice.")

        recipient_str = self._owner_jid.strip()
        if not CANONICAL_PN_REGEX.match(recipient_str):
            raise TargetNotAllowedError(f"Owner JID '{recipient_str}' is not a canonical phone number JID.")

        text_str = str(text or "").strip()
        if not text_str:
            raise ValueError("Owner notice text cannot be empty.")
        if len(text_str) > 4000:
            raise ValueError("Owner notice text exceeds 4000 characters limit.")

        _check_no_secrets(text_str)

        cid = correlation_id or str(uuid4())
        evt = threading.Event()
        res_box: Dict[str, Any] = {}
        with self._pending_lock:
            self._pending_sends[cid] = (evt, res_box)

        if self._process.stdin is None or self._process.stdin.closed:
            with self._pending_lock:
                self._pending_sends.pop(cid, None)
            raise TransportError("Sidecar stdin is closed or unavailable.")

        envelope = SidecarEnvelope(
            type=SidecarEventType.OUTBOUND_SEND,
            correlation_id=cid,
            payload={"to": recipient_str, "text": text_str},
        )
        try:
            self._process.stdin.write(serialize_sidecar_envelope(envelope))
            self._process.stdin.flush()
        except Exception as exc:
            with self._pending_lock:
                self._pending_sends.pop(cid, None)
            raise TransportError(f"Failed to write outbound envelope to sidecar stdin: {exc}") from exc

        signaled = evt.wait(timeout=timeout)
        with self._pending_lock:
            self._pending_sends.pop(cid, None)

        if not signaled:
            raise SidecarTimeoutError(f"Timed out waiting for send ack for correlation_id={cid}")

        resp_type = res_box.get("type")
        payload = res_box.get("payload", {})
        if resp_type == "error":
            err_code = payload.get("error_code") or payload.get("code") or payload.get("error", "")
            if err_code == "TARGET_NOT_ALLOWED":
                raise TargetNotAllowedError(f"Bridge rejected target: {payload.get('error', err_code)}")
            elif err_code == "RATE_LIMITED":
                raise RateLimitedError(f"Bridge rate limited send: {payload.get('error', err_code)}")
            else:
                raise GatewayError(f"Bridge reported error: {payload.get('error', err_code)}")

        msg_id = payload.get("id") or payload.get("message_id")
        if msg_id and self._echo_registry is not None:
            self._echo_registry.register(str(msg_id))

        return payload
