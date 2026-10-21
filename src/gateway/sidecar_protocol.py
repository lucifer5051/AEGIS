"""SAGE Remote Control Gateway -- WhatsApp Baileys Sidecar IPC Protocol.

Canonical Reference:
    docs/PHASE_9B_WHATSAPP_ARCHITECTURE.md § 3, § 7
    AEGIS Prompt 035 (Phase 9C.2)

Invariants:
- Unidirectional, newline-delimited JSON (JSON-Lines) over stdin/stdout.
- Stderr is reserved strictly for human-readable diagnostic logging (never mixed with JSON-RPC).
- Zero local network ports are exposed.
- All envelope payloads are untrusted and strictly bounded (max line length 64KB, max text 4096).
- Zero-Secret Invariant: secrets must never appear in envelopes, errors, or logs.
- Versioned protocol ("1.0"); unsupported versions fail closed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Dict
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.gateway.errors import GatewayError
from src.trust.audit import _check_no_secrets

MAX_LINE_BYTES = 65536  # 64 KB maximum line length
MAX_TEXT_LENGTH = 4096  # 4 KB maximum text content length
PROTOCOL_VERSION = "1.0"

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"bot\d+:[A-Za-z0-9_-]+"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
]


class SidecarProtocolError(GatewayError):
    """Raised when sidecar protocol encoding or validation fails."""


@unique
class SidecarEventType(str, Enum):
    """Typed events exchanged between SAGE Python gateway and Baileys Node sidecar."""

    INBOUND_MESSAGE = "inbound_message"
    OUTBOUND_SEND = "outbound_send"
    OUTBOUND_ACK = "outbound_ack"
    CONNECTION_UPDATE = "connection_update"
    HEARTBEAT = "heartbeat"
    SHUTDOWN = "shutdown"
    ERROR = "error"


class SidecarEnvelope(BaseModel):
    """Versioned envelope wrapping every sidecar IPC message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(
        default=PROTOCOL_VERSION, description="Protocol major.minor version identifier."
    )
    type: SidecarEventType = Field(description="Typed message action or notification.")
    correlation_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Correlation identifier linking requests to acks or responses.",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 UTC timestamp.",
    )
    payload: Dict[str, Any] = Field(
        default_factory=dict, description="Structured event-specific payload dictionary."
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
    def validate_no_secrets_in_payload(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        payload_str = json.dumps(v, sort_keys=True)
        _check_no_secrets(payload_str)
        for pat in _SECRET_PATTERNS:
            if pat.search(payload_str):
                raise ValueError("Sidecar payload violates zero-secret invariant.")
        return v


def parse_sidecar_line(line: str) -> SidecarEnvelope:
    """Parse and validate a single newline-delimited JSON line from the sidecar IPC stream.

    Args:
        line: Raw string read from child process stdout or stdin.

    Returns:
        Validated SidecarEnvelope instance.

    Raises:
        SidecarProtocolError: If line exceeds max length, is malformed JSON, or fails validation.
    """
    if not line or not line.strip():
        raise SidecarProtocolError("Received empty or whitespace-only sidecar line.")

    line_bytes = len(line.encode("utf-8"))
    if line_bytes > MAX_LINE_BYTES:
        raise SidecarProtocolError(
            f"Sidecar message exceeded maximum length "
            f"({line_bytes} bytes > {MAX_LINE_BYTES} bytes limit)."
        )

    try:
        raw_dict = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SidecarProtocolError(f"Malformed JSON in sidecar IPC message: {exc}") from exc

    if not isinstance(raw_dict, dict):
        raise SidecarProtocolError("Sidecar IPC message must be a JSON object.")

    # Check for raw secrets before Pydantic parsing
    raw_str = line.strip()
    for pat in _SECRET_PATTERNS:
        if pat.search(raw_str):
            raise SidecarProtocolError("Sidecar IPC message violates zero-secret invariant.")

    try:
        return SidecarEnvelope.model_validate(raw_dict)
    except Exception as exc:
        raise SidecarProtocolError(f"Sidecar envelope schema validation failed: {exc}") from exc


def serialize_sidecar_envelope(envelope: SidecarEnvelope) -> str:
    """Serialize a SidecarEnvelope to a newline-terminated JSON-line for IPC dispatch.

    Args:
        envelope: Validated SidecarEnvelope.

    Returns:
        Newline-terminated JSON string suitable for stdin.write().
    """
    dumped = envelope.model_dump_json()
    _check_no_secrets(dumped)
    for pat in _SECRET_PATTERNS:
        if pat.search(dumped):
            raise SidecarProtocolError("Serialized sidecar message violates zero-secret invariant.")
    return f"{dumped}\n"
