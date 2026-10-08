"""SAGE Skills Subsystem -- Typed Error Hierarchy.

Defines all domain exception types for skill manifests, validation,
registration, lifecycle transitions, tool binding enforcement,
resource admission, and execution.
"""


class SkillError(Exception):
    """Base exception for all errors originating within the skills subsystem."""


class SkillManifestValidationError(SkillError):
    """Raised when a SkillManifest violates schema, format, or security invariants."""


class SkillRegistrationError(SkillError):
    """Raised when registering or unregistering a skill fails."""


class SkillDuplicateError(SkillRegistrationError):
    """Raised when attempting to register a skill with an ID that already exists."""


class SkillNotFoundError(SkillRegistrationError):
    """Raised when a requested skill is not found in the registry."""


class SkillLifecycleError(SkillError):
    """Raised when an illegal lifecycle state transition is attempted."""


class SkillToolBindingViolationError(SkillError):
    """Raised when a skill attempts to access or bind a tool outside its manifest."""


class SkillToolAccessDeniedError(SkillToolBindingViolationError):
    """Raised when a skill execution attempts to invoke an unauthorized tool."""


class SkillResourceAdmissionError(SkillError):
    """Raised when skill admission is rejected due to resource/concurrency limits."""


class SkillExecutionError(SkillError):
    """Raised when an error occurs during skill execution."""


class ClarificationError(SkillError):
    """Raised when an error occurs during clarification gate evaluation."""


class FeedbackError(SkillError):
    """Raised when an error occurs in the experience feedback subsystem."""
