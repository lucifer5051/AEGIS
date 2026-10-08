"""SAGE Local Reasoning Subsystem - Error Hierarchy.

All errors inherit from ReasoningError.
Fail-closed philosophy: all failures prevent unauthorized model activation
and return typed, inspectable failure states.
"""


class ReasoningError(Exception):
    """Base exception for all local reasoning subsystem failures."""


class ReasoningStorageError(ReasoningError):
    """Raised when model location violates forbidden boundaries (OneDrive, Repo, C:\\AEGIS)."""


class ReasoningIntegrityError(ReasoningError):
    """Raised when model artifact byte size or SHA-256 hash fails verification."""


class ReasoningAdmissionError(ReasoningError):
    """Raised when model resource admission is denied by the SkillConcurrencyManager."""


class ReasoningExecutionError(ReasoningError):
    """Raised when model loading, execution, or token generation fails."""


class ReasoningResidencyError(ReasoningError):
    """Raised when model unloading is attempted while active skill leases exist."""
