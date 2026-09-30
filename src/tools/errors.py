"""SAGE Tool Broker -- Domain Exceptions.

All exceptions encode a fail-closed outcome.  Any caller that catches
these exceptions MUST NOT execute the requested tool action.

Invariants:
- No raw secrets, credentials, prompts, or user data appear in messages.
- All exceptions are unchecked (RuntimeError subclasses).
- Catching a ToolBrokerError MUST result in the action being denied.
"""


class ToolBrokerError(RuntimeError):
    """Base exception for all Tool Broker failures.

    All subclasses imply fail-closed: the requested tool execution is DENIED
    when any ToolBrokerError propagates to the execution boundary.
    """


# ---------------------------------------------------------------------------
# Registration errors
# ---------------------------------------------------------------------------


class ToolRegistrationError(ToolBrokerError):
    """Raised when a tool definition fails validation during registration."""


class ToolNotFoundError(ToolBrokerError):
    """Raised when a requested tool name is not registered in the broker."""


class DuplicateToolError(ToolBrokerError):
    """Raised when a tool name is registered more than once."""


# ---------------------------------------------------------------------------
# Argument validation errors
# ---------------------------------------------------------------------------


class ToolArgumentError(ToolBrokerError):
    """Raised when tool arguments fail schema validation."""


class ToolArgumentTypeError(ToolArgumentError):
    """Raised when a tool argument has the wrong type."""


class ToolArgumentMissingError(ToolArgumentError):
    """Raised when a required tool argument is absent."""


class ToolArgumentBoundsError(ToolArgumentError):
    """Raised when a tool argument value is outside permitted bounds."""


# ---------------------------------------------------------------------------
# Execution errors
# ---------------------------------------------------------------------------


class ToolExecutionError(ToolBrokerError):
    """Raised when tool execution fails at the subprocess or I/O level."""


class ToolTimeoutError(ToolExecutionError):
    """Raised when a tool execution exceeds its configured timeout."""


class ToolSandboxError(ToolExecutionError):
    """Raised when an execution sandbox constraint is violated."""


# ---------------------------------------------------------------------------
# Authorization errors (proxied from Trust Core)
# ---------------------------------------------------------------------------


class ToolAuthorizationError(ToolBrokerError):
    """Raised when the Trust Core denies capability issuance or consumption.

    This wraps TrustError subclasses so that Tool Broker callers do not need
    to import the trust module directly to catch authorization failures.
    """


# ---------------------------------------------------------------------------
# Output sanitization errors
# ---------------------------------------------------------------------------


class OutputSanitizationError(ToolBrokerError):
    """Raised when tool output fails sanitization and cannot be returned.

    This is a fail-closed response: if output cannot be safely scrubbed,
    it is suppressed and this exception is raised.
    """
