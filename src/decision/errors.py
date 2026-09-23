"""AEGIS Fast Decision Engine - Domain Exceptions.

Document Reference: docs/specifications/AEGIS_FAST_DECISION_ENGINE.md
Constraints:
- Fast Decision is purely advisory; exceptions must fail closed to safe fallback.
- No sensitive user input, tokens, or credentials may be leaked into exception messages.
"""


class FastDecisionError(Exception):
    """Base exception for all Fast Decision Engine operations."""


class ProviderUnavailableError(FastDecisionError):
    """Raised when a fast decision provider cannot be loaded or is in DISABLED/ERROR state."""


class ModelIntegrityError(FastDecisionError):
    """Base exception for model artifact validation and verification failures."""


class MissingModelFileError(ModelIntegrityError):
    """Raised when a required model file specified in the manifest is absent."""


class ChecksumMismatchError(ModelIntegrityError):
    """Raised when an artifact's computed SHA-256 hash does not match the trusted manifest."""


class HardwareConstraintError(FastDecisionError):
    """Raised when measured host hardware resources cannot safely satisfy model requirements."""


class TokenBudgetExceededError(FastDecisionError):
    """Raised when request input exceeds the maximum sequence token budget ceiling."""


class AdmissionBusyError(FastDecisionError):
    """Raised when provider concurrency limit is reached and non-blocking admission rejects wait."""


class InferenceTimeoutError(FastDecisionError):
    """Raised when inference execution exceeds the specified latency deadline."""


class LowConfidenceError(FastDecisionError):
    """Raised when provider confidence is below the qualified route acceptance threshold."""


class SecurityViolationError(FastDecisionError):
    """Raised when an unauthorized capability, schema tampering,
    or security invariant breach is detected."""
