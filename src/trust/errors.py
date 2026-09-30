"""AEGIS Trust Core -- Domain Exceptions.

All exceptions in this module encode a fail-closed outcome.  Any caller that
catches these exceptions MUST NOT grant the requested capability as a
consequence of handling the error.

Invariants:
- No secrets (credentials, keys, tokens, raw prompts) appear in exception
  messages.  Use opaque identifiers (UUIDs, SHA-256 digests) only.
- All exceptions are unchecked (inheriting from RuntimeError via TrustError).
  Callers must opt-in to recovery explicitly.
"""


class TrustError(RuntimeError):
    """Base exception for all Trust Core failures.

    All subclasses imply fail-closed: the requested action is DENIED when any
    TrustError propagates to the authorization boundary.
    """


# ---------------------------------------------------------------------------
# Capability errors
# ---------------------------------------------------------------------------


class CapabilityError(TrustError):
    """Base for capability-lifecycle failures."""


class CapabilityForgedError(CapabilityError):
    """Raised when a presented capability token fails HMAC verification.

    This indicates either a bug, tampering, or token reuse across process
    boundaries.  The capability is unconditionally rejected.
    """


class CapabilityExpiredError(CapabilityError):
    """Raised when a presented capability token is past its TTL."""


class CapabilityRevokedError(CapabilityError):
    """Raised when a presented capability token has been explicitly revoked."""


class CapabilityExhaustedError(CapabilityError):
    """Raised when a use-limited capability has been consumed to its maximum."""


class CapabilityScopeError(CapabilityError):
    """Raised when an action target is outside the capability apos;s declared scope."""


# ---------------------------------------------------------------------------
# Authorization errors
# ---------------------------------------------------------------------------


class AuthorizationError(TrustError):
    """Base for authorization-decision failures."""


class PolicyDeniedError(AuthorizationError):
    """Raised when policy evaluation results in an explicit DENY decision."""


class ApprovalRequiredError(AuthorizationError):
    """Raised when the current autonomy tier requires explicit user approval.

    The caller must persist an approval_requests row and wait for user
    confirmation before retrying.  Retrying without approval will fail again.
    """

    def __init__(self, message: str, request_id: str) -> None:
        super().__init__(message)
        self.request_id = request_id


class ApprovalExpiredError(AuthorizationError):
    """Raised when a previously issued approval request has timed out."""


class ApprovalRejectedError(AuthorizationError):
    """Raised when the user explicitly rejected an approval request."""


# ---------------------------------------------------------------------------
# Audit errors
# ---------------------------------------------------------------------------


class AuditError(TrustError):
    """Raised when the audit log writer fails to persist a record.

    Fail-closed: if auditing cannot be completed, the action must not proceed.
    """


class SecretLeakError(AuditError):
    """Raised when a telemetry payload is detected to contain a raw secret.

    This is an internal invariant violation; it is never expected in normal
    operation and must trigger an immediate alert and halt.
    """


# ---------------------------------------------------------------------------
# Policy-configuration errors
# ---------------------------------------------------------------------------


class PolicyConfigurationError(TrustError):
    """Raised when Trust Core policy is malformed, ambiguous, or internally
    inconsistent.  Policy misconfiguration is always fail-closed."""
