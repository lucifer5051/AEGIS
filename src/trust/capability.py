"""AEGIS Trust Core -- Opaque, Non-Forgeable Capability Tokens.

Design:
- A Capability is a *short-lived, single-purpose, HMAC-signed token* that
  authorises exactly one class of action on a declared resource scope.
- Tokens are opaque to callers: they carry no embedded plaintext claims that
  a caller could reason about or manipulate.
- The signing key is a process-local secret generated once at start-up and
  never logged, serialised, or written to any external store.
- Verification is constant-time (hmac.compare_digest) to prevent timing oracles.
- Revocation is tracked in an in-process set keyed by capability_id.

Constraints:
- Capabilities are not persistable across process restarts by design.
- No raw secrets, user prompts, or credentials may appear in a Capability.
- This module has ZERO external dependencies (stdlib only).
"""

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import FrozenSet, Optional

from src.trust.errors import (
    CapabilityExhaustedError,
    CapabilityExpiredError,
    CapabilityForgedError,
    CapabilityRevokedError,
    CapabilityScopeError,
)

# ---------------------------------------------------------------------------
# Action-risk classification (deterministic, no LLM inference)
# ---------------------------------------------------------------------------


@unique
class ActionRisk(str, Enum):
    """Canonical action-risk tiers.

    Strictly separate from autonomy tiers (0-3).  Risk is an intrinsic
    property of the action type; autonomy tier is an operational policy
    configuration.
    """

    LOW = "LOW"  # Read-only, non-mutating, sandbox-safe
    MEDIUM = "MEDIUM"  # Local state mutation, reversible
    HIGH = "HIGH"  # External or irreversible local mutation
    CRITICAL = "CRITICAL"  # Destructive, credential-touching, or network-external


# ---------------------------------------------------------------------------
# Resource scope descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceScope:
    """Declares the boundary of resources a capability may touch.

    All fields are *allowlists*.  An empty frozenset means the corresponding
    resource class is not permitted at all (fail-closed default).

    Attributes:
        filesystem_prefixes:  Normalised absolute path prefixes the action may
            read/write.  The Trust Core normalises to posix-style and checks
            with str.startswith before issuing the capability.
        allowed_network_hosts: Explicit hostnames (no wildcards) the action may
            contact.  Empty = no network access.
        allowed_process_commands: Explicit command names (basename only) the
            action may spawn.  Empty = no subprocess allowed.
        project_id: If set, scope is restricted to resources belonging to this
            project.  None means system-global scope (rare, requires CRITICAL
            or explicit user approval).
    """

    filesystem_prefixes: FrozenSet[str] = field(default_factory=frozenset)
    allowed_network_hosts: FrozenSet[str] = field(default_factory=frozenset)
    allowed_process_commands: FrozenSet[str] = field(default_factory=frozenset)
    project_id: Optional[str] = None

    def contains_path(self, path: str) -> bool:
        """Return True iff *path* is within an allowed filesystem prefix.

        Normalises the path to resolve ``..`` and ``//`` sequences *before*
        prefix matching to prevent path-traversal bypass attacks.  Windows
        backslashes are converted to forward slashes prior to normalization.
        """
        import posixpath

        norm = posixpath.normpath(path.replace("\\", "/"))
        # Ensure prefix ends without trailing slash for consistent comparison
        return any(
            norm == p.rstrip("/") or norm.startswith(p.rstrip("/") + "/")
            for p in self.filesystem_prefixes
        )

    def contains_host(self, host: str) -> bool:
        """Return True iff *host* is in the network allowlist."""
        return host.lower() in {h.lower() for h in self.allowed_network_hosts}

    def contains_command(self, command: str) -> bool:
        """Return True iff *command* basename is in the process allowlist."""
        basename = os.path.basename(command)
        return basename in self.allowed_process_commands


# ---------------------------------------------------------------------------
# Capability token (opaque to callers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """Opaque, HMAC-signed, short-lived authorisation token.

    Fields are read-only after construction.  The HMAC *signature* field binds
    all other fields; any mutation would invalidate it.

    ``capability_id`` is an opaque UUID string (no embedded claims).
    ``action_name`` is the canonical action label (e.g. "filesystem.read").
    ``risk``        is the pre-classified action risk.
    ``scope``       declares the resource boundary.
    ``issued_at``   is a monotonic POSIX timestamp (float).
    ``ttl_seconds`` is the maximum lifetime.  Default: 60 s.
    ``max_uses``    is the maximum number of times the capability may be
                    consumed.  0 = unlimited (use with care for CRITICAL risk).
    ``signature``   is the HMAC-SHA256 of the canonical payload; set by the
                    CapabilityFactory and never set externally.
    """

    capability_id: str
    action_name: str
    risk: ActionRisk
    scope: ResourceScope
    issued_at: float
    ttl_seconds: float
    max_uses: int  # 0 = unlimited
    signature: bytes  # HMAC-SHA256; must be verified before use

    def is_expired(self, now: Optional[float] = None) -> bool:
        """Return True if the token TTL has elapsed."""
        t = now if now is not None else time.time()
        return (t - self.issued_at) > self.ttl_seconds

    def _canonical_payload(self) -> bytes:
        """Deterministic serialisation of all signed fields (no signature)."""
        obj = {
            "capability_id": self.capability_id,
            "action_name": self.action_name,
            "risk": self.risk.value,
            "scope": {
                "filesystem_prefixes": sorted(self.scope.filesystem_prefixes),
                "allowed_network_hosts": sorted(self.scope.allowed_network_hosts),
                "allowed_process_commands": sorted(self.scope.allowed_process_commands),
                "project_id": self.scope.project_id,
            },
            "issued_at": self.issued_at,
            "ttl_seconds": self.ttl_seconds,
            "max_uses": self.max_uses,
        }
        return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Factory & Registry
# ---------------------------------------------------------------------------


class CapabilityFactory:
    """Issues and verifies AEGIS capability tokens.

    A single factory instance holds the process-local signing key.  It is
    never serialised and never reused across process restarts.

    Thread-safety: factory.issue() and factory.verify() are stateless w.r.t.
    the signing key; the revocation set is guarded by the caller
    (CapabilityRegistry handles concurrent revocation safely via a Python set
    which is GIL-protected for add/discard/in operations).
    """

    def __init__(self) -> None:
        # 32 bytes of OS-provided randomness.  Never written anywhere.
        self._key: bytes = os.urandom(32)

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()

    def issue(
        self,
        capability_id: str,
        action_name: str,
        risk: ActionRisk,
        scope: ResourceScope,
        ttl_seconds: float = 60.0,
        max_uses: int = 1,
    ) -> "Capability":
        """Create a signed capability token.

        Args:
            capability_id:  Caller-provided opaque UUID string.
            action_name:    Canonical action label.
            risk:           Pre-classified action risk (caller responsible).
            scope:          Resource boundary declaration.
            ttl_seconds:    Token lifetime in seconds.
            max_uses:       Max consumptions.  0 = unlimited.

        Returns:
            A frozen Capability with a valid HMAC signature.
        """
        ttl_seconds = max(0.0001, float(ttl_seconds))
        cap = Capability(
            capability_id=capability_id,
            action_name=action_name,
            risk=risk,
            scope=scope,
            issued_at=time.time(),
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
            signature=b"",  # placeholder; replaced below
        )
        # Build real signature over all other fields
        sig = self._sign(cap._canonical_payload())
        # Rebuild with real signature (frozen dataclass requires object.__setattr__)
        object.__setattr__(cap, "signature", sig)
        return cap

    def verify_signature(self, cap: "Capability") -> None:
        """Verify the HMAC of a capability.  Raises CapabilityForgedError on failure."""
        expected = self._sign(cap._canonical_payload())
        if not hmac.compare_digest(expected, cap.signature):
            raise CapabilityForgedError(
                f"Capability {cap.capability_id!r}: HMAC verification failed. "
                "Token is invalid or has been tampered with."
            )


class CapabilityRegistry:
    """Tracks issued capabilities, revocations, and use-counts.

    In-process only.  All state is lost on process restart (by design: tokens
    are short-lived and not meant to survive restarts).
    """

    def __init__(self, factory: CapabilityFactory) -> None:
        self._factory = factory
        self._revoked: set[str] = set()
        self._use_counts: dict[str, int] = {}

    def issue(
        self,
        capability_id: str,
        action_name: str,
        risk: ActionRisk,
        scope: ResourceScope,
        ttl_seconds: float = 60.0,
        max_uses: int = 1,
    ) -> Capability:
        """Issue and register a new capability token."""
        cap = self._factory.issue(
            capability_id=capability_id,
            action_name=action_name,
            risk=risk,
            scope=scope,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
        )
        self._use_counts[capability_id] = 0
        return cap

    def revoke(self, capability_id: str) -> None:
        """Immediately revoke a capability by ID."""
        self._revoked.add(capability_id)

    def consume(
        self,
        cap: Capability,
        target_path: Optional[str] = None,
        target_host: Optional[str] = None,
        target_command: Optional[str] = None,
    ) -> None:
        """Validate and consume one use of the capability.

        Performs (in order):
        1. HMAC signature verification.
        2. Expiry check.
        3. Revocation check.
        4. Use-count check.
        5. Scope check (path / host / command).
        6. Increment use-count.

        All failures raise the appropriate CapabilityError subclass.
        """
        # 1. Signature
        self._factory.verify_signature(cap)

        # 2. Expiry
        if cap.is_expired():
            raise CapabilityExpiredError(
                f"Capability {cap.capability_id!r} has expired (TTL={cap.ttl_seconds}s)."
            )

        # 3. Revocation
        if cap.capability_id in self._revoked:
            raise CapabilityRevokedError(f"Capability {cap.capability_id!r} has been revoked.")

        # 4. Use-count
        current_uses = self._use_counts.get(cap.capability_id, 0)
        if cap.max_uses > 0 and current_uses >= cap.max_uses:
            raise CapabilityExhaustedError(
                f"Capability {cap.capability_id!r} exhausted (max_uses={cap.max_uses})."
            )

        # 5. Scope
        if target_path is not None and not cap.scope.contains_path(target_path):
            raise CapabilityScopeError(
                f"Capability {cap.capability_id!r}: path is outside declared scope."
            )
        if target_host is not None and not cap.scope.contains_host(target_host):
            raise CapabilityScopeError(
                f"Capability {cap.capability_id!r}: host is outside declared scope."
            )
        if target_command is not None and not cap.scope.contains_command(target_command):
            raise CapabilityScopeError(
                f"Capability {cap.capability_id!r}: command is outside declared scope."
            )

        # 6. Increment use-count
        self._use_counts[cap.capability_id] = current_uses + 1
