"""AEGIS Trust Core -- Append-Only Audit Log Writer.

Zero-Secret Invariant: This module MUST NEVER write passwords, OAuth tokens,
API keys, encryption keys, or raw user prompts to the audit log.

Only opaque references are acceptable:
  - SHA-256 digests of secrets (not the secrets themselves)
  - Capability IDs (opaque UUIDs)
  - Project IDs / entity IDs

All audit writes are atomic (WAL + BEGIN IMMEDIATE).  If the write fails,
AuditError is raised so the caller can abort the in-flight action.

The `audit_records` table is defined in migration 0005_trust_core_schema.sql.
"""

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum, unique
from typing import Any, Dict, Optional
from uuid import uuid4

from src.trust.errors import AuditError, SecretLeakError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret-leak detector
# ---------------------------------------------------------------------------

# Patterns that must NEVER appear in audit telemetry JSON.
# These are conservative heuristics; the list should grow over time.
_SECRET_PATTERNS = [
    # Match both JSON-serialized form ("password":"value") and plain text (password = value).
    # The \"? prefix/suffix handles JSON double-quote wrapping.
    re.compile(r'(?i)"?password"?\s*[=:]\s*"?\S+'),
    re.compile(r'(?i)"?api[_-]?key"?\s*[=:]\s*"?\S+'),
    re.compile(r'(?i)"?secret"?\s*[=:]\s*"?\S+'),
    re.compile(r'(?i)"?token"?\s*[=:]\s*"?\S+'),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r'(?i)"?authorization"?\s*[=:]\s*"?\S+'),
    # AWS/GCP/Azure style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r'(?i)"?private[_-]?key"?'),
]


def _check_no_secrets(telemetry: str) -> None:
    """Raise SecretLeakError if *telemetry* appears to contain raw secrets."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(telemetry):
            raise SecretLeakError(
                "Audit telemetry payload matched a secret-pattern heuristic. "
                "Audit write aborted to protect secret confidentiality. "
                f"Pattern: {pattern.pattern!r}"
            )


# ---------------------------------------------------------------------------
# Audit event types (canonical, matches DB CHECK constraint)
# ---------------------------------------------------------------------------


@unique
class AuditEventType(str, Enum):
    AUTHZ_DECISION = "AUTHZ_DECISION"
    CAPABILITY_ISSUED = "CAPABILITY_ISSUED"
    CAPABILITY_REVOKED = "CAPABILITY_REVOKED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    POLICY_CHANGE = "POLICY_CHANGE"
    DESTRUCTIVE_OP = "DESTRUCTIVE_OP"
    PROJECT_PURGE = "PROJECT_PURGE"
    BACKUP_CREATED = "BACKUP_CREATED"
    RESTORE_EXECUTED = "RESTORE_EXECUTED"
    CREDENTIAL_ACCESSED = "CREDENTIAL_ACCESSED"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"


@unique
class AuditDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


# ---------------------------------------------------------------------------
# Audit record dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRecord:
    """Immutable representation of one audit record before DB persistence."""

    audit_id: str
    event_type: AuditEventType
    actor: str  # Caller identity (e.g. "fast_decision_service")
    target_entity_type: str  # e.g. "capability", "project", "file"
    target_entity_id: str  # Opaque ID; never a raw secret
    project_id: Optional[str]
    decision: AuditDecision
    policy_rule_id: str
    telemetry: Dict[str, Any]  # Context without secrets
    timestamp: float  # POSIX time (float)


# ---------------------------------------------------------------------------
# Audit writer
# ---------------------------------------------------------------------------


class AuditWriter:
    """Writes tamper-resistant, append-only audit records to the SQLite store.

    The writer does NOT manage the DB connection lifecycle; it receives an open
    sqlite3.Connection and uses it.  Callers must wrap in open_database().

    Fail-closed: if any write fails, AuditError is raised.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def write(
        self,
        event_type: AuditEventType,
        actor: str,
        target_entity_type: str,
        target_entity_id: str,
        decision: AuditDecision,
        policy_rule_id: str,
        telemetry: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
    ) -> AuditRecord:
        """Persist one audit record atomically.

        Args:
            event_type:          One of AuditEventType.*
            actor:               Caller / component identity string.
            target_entity_type:  Resource type being audited.
            target_entity_id:    Opaque ID of the resource.
            decision:            ALLOW / DENY / ESCALATE.
            policy_rule_id:      The rule_id from PolicyResult for traceability.
            telemetry:           Optional context dict.  Secret-leak check runs
                                 before write.  Keys/values must be opaque.
            project_id:          Optional owning project.

        Returns:
            The persisted AuditRecord.

        Raises:
            SecretLeakError:  If telemetry contains suspicious secret-like content.
            AuditError:       If DB write fails for any reason.
        """
        telemetry = telemetry or {}
        telemetry_json = json.dumps(telemetry, sort_keys=True, separators=(",", ":"))

        # Zero-secret invariant enforcement
        _check_no_secrets(telemetry_json)

        audit_id = str(uuid4())
        timestamp = time.time()
        ts_iso = _posix_to_iso(timestamp)

        record = AuditRecord(
            audit_id=audit_id,
            event_type=event_type,
            actor=actor,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            project_id=project_id,
            decision=decision,
            policy_rule_id=policy_rule_id,
            telemetry=telemetry,
            timestamp=timestamp,
        )

        try:
            self._conn.execute("BEGIN IMMEDIATE;")
            self._conn.execute(
                """
                INSERT INTO audit_records (
                    audit_id, event_type, actor,
                    target_entity_type, target_entity_id,
                    project_id, decision, policy_rule_id,
                    telemetry_json, timestamp
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    audit_id,
                    event_type.value,
                    actor,
                    target_entity_type,
                    target_entity_id,
                    project_id,
                    decision.value,
                    policy_rule_id,
                    telemetry_json,
                    ts_iso,
                ),
            )
            self._conn.execute("COMMIT;")
        except Exception as exc:
            try:
                self._conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise AuditError(f"Failed to persist audit record: {exc}") from exc

        logger.debug(
            "Audit [%s] %s/%s -> %s (rule=%s)",
            event_type.value,
            target_entity_type,
            target_entity_id,
            decision.value,
            policy_rule_id,
        )
        return record


def redact_secret_hash(value: str) -> str:
    """Replace a raw secret with its SHA-256 digest for safe audit logging.

    Use this helper when you need to log *that* a credential was used without
    logging the credential itself.
    """
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _posix_to_iso(ts: float) -> str:
    """Format a POSIX float timestamp as a UTC ISO-8601 string."""
    import datetime

    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
