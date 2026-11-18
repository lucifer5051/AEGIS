"""SAGE Friday Communication Gateway -- Secure Sidecar IPC Protocol.

Canonical Reference:
    PROMPT 072: SAGE Friday Communication Gateway & Secure Sidecar Protocol
    PROMPT 073: SAGE Application Runtime Integration & Communication Security Hardening

Invariants:
- Unidirectional, newline-delimited JSON (JSON-Lines) over standard I/O pipes.
- Zero network sockets or ports exposed.
- All envelope payloads are untrusted and bounded (max line length 64KB, max text 4096).
- Mandatory handshake with pre-shared secret authentication.
- Cryptographically authenticated canonical message representation:
  version | event_type | correlation_id | timestamp | nonce | canonical_payload
- Constant-time HMAC comparison (hmac.compare_digest).
- Bounded time-window expiration and sliding-window replay defense.
- Zero-Secret Invariant: Tokens, keys, and private data scrubbed from logs and errors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from enum import Enum, unique
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.communication.errors import (
    SidecarSecurityError,
    SidecarTimeoutError,
)
from src.models.errors import redact_secrets_from_text
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)

MAX_IPC_LINE_BYTES: int = 65536  # 64 KB limit
MAX_TEXT_CONTENT_LENGTH: int = 4096
PROTOCOL_VERSION: str = "2.0"
MAX_MESSAGE_TIME_DRIFT_SECONDS: float = 120.0  # 2 minute max drift
REPLAY_WINDOW_SECONDS: float = 300.0  # 5 minute replay cache window


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def generate_sidecar_secret() -> str:
    """Generate a cryptographically secure 256-bit authentication token for sidecar IPC."""
    return secrets.token_hex(32)


@unique
class SidecarEventType(str, Enum):
    """Permitted typed events exchanged across the secure sidecar IPC boundary."""

    REGISTER = "register"
    REGISTER_ACK = "register_ack"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    INBOUND_MESSAGE = "inbound_message"
    OUTBOUND_SEND = "outbound_send"
    OUTBOUND_ACK = "outbound_ack"
    SHUTDOWN = "shutdown"
    ERROR = "error"


class SidecarEnvelope(BaseModel):
    """Hardened versioned envelope wrapping every sidecar IPC message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(
        default=PROTOCOL_VERSION, description="Protocol major.minor version identifier."
    )
    type: SidecarEventType = Field(description="Typed IPC action or event.")
    correlation_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Correlation ID linking requests to responses/acks.",
    )
    nonce: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique message nonce for replay defense.",
    )
    auth_digest: Optional[str] = Field(
        default=None, description="HMAC-SHA256 signature for complete message authentication."
    )
    timestamp: str = Field(
        default_factory=_utc_now_iso, description="ISO-8601 UTC timestamp."
    )
    payload: Dict[str, Any] = Field(
        default_factory=dict, description="Structured event payload."
    )

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        if v != PROTOCOL_VERSION:
            raise ValueError(
                f"Unsupported sidecar protocol version '{v}'. Expected '{PROTOCOL_VERSION}'."
            )
        return v

    @field_validator("payload")
    @classmethod
    def validate_payload_safety(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        payload_str = json.dumps(v, sort_keys=True)
        _check_no_secrets(payload_str)
        return v


def compute_auth_digest(
    secret: str,
    version: str,
    event_type: str,
    correlation_id: str,
    timestamp: str,
    nonce: str,
    payload: Dict[str, Any],
) -> str:
    """Compute HMAC-SHA256 digest over canonical representation of all envelope fields including payload.

    Canonical representation:
        version | event_type | correlation_id | timestamp | nonce | canonical_payload
    """
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    canonical_string = f"{version}|{event_type}|{correlation_id}|{timestamp}|{nonce}|{canonical_payload}"
    return hmac.new(secret.encode("utf-8"), canonical_string.encode("utf-8"), hashlib.sha256).hexdigest()


class SecureSidecarCodec:
    """Encodes and decodes newline-delimited sidecar messages with boundary validation and cryptographic authentication."""

    def __init__(
        self,
        auth_secret: Optional[str] = None,
        max_drift_seconds: float = MAX_MESSAGE_TIME_DRIFT_SECONDS,
    ) -> None:
        self._auth_secret = auth_secret
        self._max_drift = max_drift_seconds

    def encode(self, envelope: SidecarEnvelope) -> str:
        """Serialize a SidecarEnvelope to a newline-terminated JSON string with complete cryptographic signature."""
        if self._auth_secret and not envelope.auth_digest:
            nonce = envelope.nonce or str(uuid4())
            digest = compute_auth_digest(
                secret=self._auth_secret,
                version=envelope.version,
                event_type=envelope.type.value,
                correlation_id=envelope.correlation_id,
                timestamp=envelope.timestamp,
                nonce=nonce,
                payload=envelope.payload,
            )
            # Reconstruct signed immutable envelope
            envelope = SidecarEnvelope(
                version=envelope.version,
                type=envelope.type,
                correlation_id=envelope.correlation_id,
                nonce=nonce,
                auth_digest=digest,
                timestamp=envelope.timestamp,
                payload=envelope.payload,
            )

        data = envelope.model_dump_json()
        raw_bytes = data.encode("utf-8")
        if len(raw_bytes) > MAX_IPC_LINE_BYTES:
            raise SidecarSecurityError(
                f"Outbound IPC message size {len(raw_bytes)} exceeds {MAX_IPC_LINE_BYTES} byte limit."
            )
        return data + "\n"

    def decode(self, line: str) -> SidecarEnvelope:
        """Parse and validate a single newline-delimited JSON line.

        Raises:
            SidecarSecurityError: On oversized payload, malformed JSON, version mismatch, expired timestamp, or bad signature.
        """
        if not line or not line.strip():
            raise SidecarSecurityError("Received empty or whitespace-only sidecar IPC line.")

        raw_bytes = line.encode("utf-8")
        if len(raw_bytes) > MAX_IPC_LINE_BYTES:
            raise SidecarSecurityError(
                f"Inbound IPC line {len(raw_bytes)} bytes exceeds {MAX_IPC_LINE_BYTES} limit."
            )

        try:
            raw_dict = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SidecarSecurityError(f"Malformed JSON in sidecar IPC message: {exc}") from exc

        if not isinstance(raw_dict, dict):
            raise SidecarSecurityError("Sidecar IPC message must be a JSON object.")

        # Pre-validation secret check
        _check_no_secrets(line.strip())

        try:
            envelope = SidecarEnvelope.model_validate(raw_dict)
        except Exception as exc:
            raise SidecarSecurityError(f"Sidecar envelope schema validation failed: {exc}") from exc

        # Timestamp freshness and expiration check
        try:
            msg_dt = _parse_iso(envelope.timestamp)
            now_dt = datetime.now(timezone.utc)
            drift = abs((now_dt - msg_dt).total_seconds())
            if drift > self._max_drift:
                raise SidecarSecurityError(
                    f"Sidecar envelope timestamp '{envelope.timestamp}' is outside allowed time window ({drift:.1f}s > {self._max_drift:.1f}s)."
                )
        except SidecarSecurityError:
            raise
        except Exception as exc:
            raise SidecarSecurityError(f"Invalid timestamp format in sidecar envelope: {exc}") from exc

        # Authenticate if secret is configured
        if self._auth_secret:
            if not envelope.auth_digest:
                raise SidecarSecurityError("Missing mandatory auth_digest on IPC envelope.")
            expected = compute_auth_digest(
                secret=self._auth_secret,
                version=envelope.version,
                event_type=envelope.type.value,
                correlation_id=envelope.correlation_id,
                timestamp=envelope.timestamp,
                nonce=envelope.nonce,
                payload=envelope.payload,
            )
            if not hmac.compare_digest(envelope.auth_digest, expected):
                raise SidecarSecurityError("Sidecar envelope HMAC authentication failure.")

        return envelope


class PersistentReplayStore:
    """Thread-safe, crash-resilient persistent replay store for sidecar nonces and correlation IDs.

    Guarantees:
    - Nonces and correlation IDs survive host process restarts.
    - Atomic check-and-insert prevents duplicate processing across threads or reconnections.
    - Bounded storage via automatic time-based purging.
    - Fail-closed semantics: any storage error or corruption raises SidecarSecurityError.
    - Plaintext secrets are NEVER written to disk or storage.
    """

    def __init__(self, db_path: Union[str, Path, sqlite3.Connection] = ":memory:") -> None:
        self._lock = threading.Lock()
        if isinstance(db_path, sqlite3.Connection):
            self._conn = db_path
            self._owns_conn = False
        else:
            self._db_path = str(db_path)
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=10.0,
            )
            self._owns_conn = True
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.Error:
                pass

        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS sidecar_replay_cache (
                            identifier TEXT PRIMARY KEY,
                            id_type TEXT NOT NULL,
                            expires_at REAL NOT NULL,
                            created_at REAL NOT NULL
                        )
                        """
                    )
                    self._conn.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_replay_expires
                        ON sidecar_replay_cache(expires_at)
                        """
                    )
                    self._conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS sidecar_sessions (
                            session_id TEXT PRIMARY KEY,
                            created_at REAL NOT NULL
                        )
                        """
                    )
            except Exception as exc:
                raise SidecarSecurityError(f"Failed to initialize persistent replay store: {exc}") from exc

    def check_and_record(self, identifier: str, id_type: str, expires_at: float) -> bool:
        """Atomically verify that identifier has not been seen and record it.

        Returns True if newly recorded, or False if already seen (replay detected).
        Raises SidecarSecurityError on any database or system error (fail-closed).
        """
        now = time.time()
        with self._lock:
            try:
                with self._conn:
                    # Bounded purge of expired records
                    self._conn.execute(
                        "DELETE FROM sidecar_replay_cache WHERE expires_at <= ?",
                        (now,),
                    )
                    # Check existing record
                    cur = self._conn.execute(
                        "SELECT 1 FROM sidecar_replay_cache WHERE identifier = ? AND expires_at > ?",
                        (identifier, now),
                    )
                    if cur.fetchone() is not None:
                        return False

                    # Insert new identifier
                    self._conn.execute(
                        """
                        INSERT INTO sidecar_replay_cache (identifier, id_type, expires_at, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (identifier, id_type, expires_at, now),
                    )
                    return True
            except sqlite3.IntegrityError:
                return False
            except Exception as exc:
                raise SidecarSecurityError(f"Replay store operation failed: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            if self._owns_conn and self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass


class SecureSidecarHost:
    """Server-side supervisor managing sidecar process handshake, heartbeats, and persistent replay defense."""

    def __init__(
        self,
        auth_secret: str,
        timeout_seconds: float = 10.0,
        replay_window_seconds: float = REPLAY_WINDOW_SECONDS,
        replay_store: Optional[PersistentReplayStore] = None,
        storage_path: Optional[Union[str, Path]] = None,
    ) -> None:
        self._auth_secret = auth_secret
        self._timeout = timeout_seconds
        self._replay_window = replay_window_seconds
        self._codec = SecureSidecarCodec(auth_secret=auth_secret)
        self._registered: bool = False
        self._sidecar_id: Optional[str] = None
        self._session_generation: int = 0
        self._session_id: str = secrets.token_hex(16)
        self._seen_nonces: Dict[str, float] = {}
        self._seen_correlations: Dict[str, float] = {}
        self._last_heartbeat: float = time.monotonic()
        self._is_active: bool = False
        self._lock = threading.Lock()

        if replay_store is not None:
            self._replay_store = replay_store
        elif storage_path is not None:
            self._replay_store = PersistentReplayStore(storage_path)
        else:
            self._replay_store = PersistentReplayStore(":memory:")

    @property
    def is_registered(self) -> bool:
        return self._registered

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def session_generation(self) -> int:
        return self._session_generation

    @property
    def session_id(self) -> str:
        return self._session_id

    def _purge_expired_cache(self, current_mono: float) -> None:
        """Purge entries that have exceeded the sliding replay window."""
        expired_nonces = [n for n, exp in self._seen_nonces.items() if exp <= current_mono]
        for n in expired_nonces:
            del self._seen_nonces[n]

        expired_corrs = [c for c, exp in self._seen_correlations.items() if exp <= current_mono]
        for c in expired_corrs:
            del self._seen_correlations[c]

    def process_incoming_line(self, line: str) -> SidecarEnvelope:
        """Process, authenticate, and validate an incoming IPC line from the child process.

        Raises:
            SidecarSecurityError: On signature mismatch, replay, expired timestamp, or unauthenticated event.
        """
        envelope = self._codec.decode(line)
        now_mono = time.monotonic()
        now_epoch = time.time()
        expires_at = now_epoch + self._replay_window

        with self._lock:
            self._purge_expired_cache(now_mono)

            # Persistent replay check 1: Nonce
            try:
                if not self._replay_store.check_and_record(envelope.nonce, "nonce", expires_at):
                    raise SidecarSecurityError(
                        f"Replay detected: nonce '{envelope.nonce}' was already accepted."
                    )
            except SidecarSecurityError:
                raise
            except Exception as exc:
                raise SidecarSecurityError(f"Replay store verification error: {exc}") from exc

            # Persistent replay check 2: Correlation ID
            try:
                if not self._replay_store.check_and_record(envelope.correlation_id, "correlation_id", expires_at):
                    raise SidecarSecurityError(
                        f"Replay detected: correlation_id '{envelope.correlation_id}' was already processed."
                    )
            except SidecarSecurityError:
                raise
            except Exception as exc:
                raise SidecarSecurityError(f"Replay store verification error: {exc}") from exc

            # In-memory fast cache check
            if envelope.nonce in self._seen_nonces:
                raise SidecarSecurityError(
                    f"Replay detected: nonce '{envelope.nonce}' was already accepted in active window."
                )
            self._seen_nonces[envelope.nonce] = now_mono + self._replay_window

            if envelope.correlation_id in self._seen_correlations:
                raise SidecarSecurityError(
                    f"Replay detected: correlation_id '{envelope.correlation_id}' was already processed in active window."
                )
            self._seen_correlations[envelope.correlation_id] = now_mono + self._replay_window

            # Process handshake
            if envelope.type == SidecarEventType.REGISTER:
                sidecar_id = envelope.payload.get("sidecar_id")
                if not sidecar_id:
                    raise SidecarSecurityError("Registration payload missing 'sidecar_id'.")
                self._registered = True
                self._sidecar_id = str(sidecar_id)
                self._session_generation += 1
                self._is_active = True
                self._last_heartbeat = time.monotonic()
                return envelope

            # Before registration, no other messages are permitted
            if not self._registered:
                raise SidecarSecurityError(
                    f"Sidecar sent event '{envelope.type.value}' before completing registration."
                )

            if envelope.type == SidecarEventType.HEARTBEAT:
                self._last_heartbeat = time.monotonic()

            return envelope

    def create_registration_ack(self, correlation_id: str) -> str:
        """Generate a REGISTER_ACK envelope for the sidecar."""
        env = SidecarEnvelope(
            type=SidecarEventType.REGISTER_ACK,
            correlation_id=correlation_id,
            payload={
                "status": "ACCEPTED",
                "version": PROTOCOL_VERSION,
                "session_generation": self._session_generation,
                "session_id": self._session_id,
            },
        )
        return self._codec.encode(env)

    def create_heartbeat_ack(self, correlation_id: str) -> str:
        """Generate a HEARTBEAT_ACK envelope."""
        env = SidecarEnvelope(
            type=SidecarEventType.HEARTBEAT_ACK,
            correlation_id=correlation_id,
            payload={"status": "OK"},
        )
        return self._codec.encode(env)

    def create_outbound_send(self, destination: str, content: str, correlation_id: Optional[str] = None) -> str:
        """Format an OUTBOUND_SEND message for the sidecar transport."""
        cid = correlation_id or str(uuid4())
        env = SidecarEnvelope(
            type=SidecarEventType.OUTBOUND_SEND,
            correlation_id=cid,
            payload={"destination": destination, "content": content},
        )
        return self._codec.encode(env)

    def create_shutdown(self) -> str:
        """Format a graceful SHUTDOWN message."""
        self._is_active = False
        env = SidecarEnvelope(
            type=SidecarEventType.SHUTDOWN,
            payload={"reason": "NORMAL_SHUTDOWN"},
        )
        return self._codec.encode(env)
