"""SAGE Tool Broker -- Provider-Independent Tool Models.

Defines the data types used to declare and describe tools.  These models
are intentionally provider-independent: they carry no LLM-specific
serialisation logic and can be used with any decision provider.

Design:
- Tools are declared statically via ToolDefinition before registration.
- ToolParameterSchema describes the allowed arguments (type, constraints).
- ToolResult is the uniform return type from all tool executions.
- No mutable state is held in these models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any, Dict, FrozenSet, Optional, Sequence

from src.tools.errors import (
    ToolArgumentBoundsError,
    ToolArgumentMissingError,
    ToolArgumentTypeError,
    ToolRegistrationError,
)
from src.trust.capability import ActionRisk, ResourceScope

# ---------------------------------------------------------------------------
# Parameter type enum
# ---------------------------------------------------------------------------

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


@unique
class ParamType(str, Enum):
    """Supported parameter types for tool argument schema."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    PATH = "path"  # Like STRING but subject to filesystem-scope validation


# ---------------------------------------------------------------------------
# Parameter schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolParameterSchema:
    """Describes one parameter accepted by a tool.

    All fields that affect validation are frozen; no mutation post-construction.

    Attributes:
        name:          Parameter name (used as the dict key in the args map).
        param_type:    Expected type of the argument value.
        required:      If True, the argument must be present in every call.
        description:   Human-readable description (shown in error messages).
        min_value:     Minimum numeric value (applies to INTEGER/FLOAT only).
        max_value:     Maximum numeric value (applies to INTEGER/FLOAT only).
        max_length:    Maximum string length (applies to STRING/PATH only).
        allowed_values: If non-empty, value must be one of these (strings only).
    """

    name: str
    param_type: ParamType
    required: bool = True
    description: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    max_length: Optional[int] = None
    allowed_values: FrozenSet[str] = field(default_factory=frozenset)

    def validate(self, raw_value: Any) -> Any:
        """Validate and coerce *raw_value* to the declared type.

        Returns the coerced value on success.

        Raises:
            ToolArgumentTypeError:   Wrong type and cannot coerce.
            ToolArgumentBoundsError: Value outside numeric/length/allowed bounds.
        """
        # --- Type coercion ---
        value: Any  # typed Any to allow int/float/bool/str assignments below
        try:
            if self.param_type == ParamType.STRING or self.param_type == ParamType.PATH:
                value = str(raw_value)
            elif self.param_type == ParamType.INTEGER:
                value = int(raw_value)
            elif self.param_type == ParamType.FLOAT:
                value = float(raw_value)
            elif self.param_type == ParamType.BOOLEAN:
                if isinstance(raw_value, bool):
                    value = raw_value
                elif isinstance(raw_value, str):
                    if raw_value.lower() in ("true", "1", "yes"):
                        value = True
                    elif raw_value.lower() in ("false", "0", "no"):
                        value = False
                    else:
                        raise ValueError(f"Cannot interpret {raw_value!r} as boolean")
                elif isinstance(raw_value, int):
                    value = bool(raw_value)
                else:
                    raise TypeError(f"Cannot coerce {type(raw_value).__name__} to boolean")
            else:
                raise ToolArgumentTypeError(
                    f"Parameter '{self.name}': unsupported param_type {self.param_type!r}."
                )
        except (ValueError, TypeError) as exc:
            raise ToolArgumentTypeError(
                f"Parameter '{self.name}': expected {self.param_type.value}, "
                f"got {type(raw_value).__name__}."
            ) from exc

        # --- Numeric bounds ---
        if self.param_type in (ParamType.INTEGER, ParamType.FLOAT):
            numeric_value: float = float(value)
            if self.min_value is not None and numeric_value < self.min_value:
                raise ToolArgumentBoundsError(
                    f"Parameter '{self.name}': value {value} is below minimum {self.min_value}."
                )
            if self.max_value is not None and numeric_value > self.max_value:
                raise ToolArgumentBoundsError(
                    f"Parameter '{self.name}': value {value} exceeds maximum {self.max_value}."
                )

        # --- String length ---
        if self.param_type in (ParamType.STRING, ParamType.PATH):
            if self.max_length is not None and len(value) > self.max_length:
                raise ToolArgumentBoundsError(
                    f"Parameter '{self.name}': string length {len(value)} "
                    f"exceeds maximum {self.max_length}."
                )

        # --- Allowed values ---
        if self.allowed_values and str(value) not in self.allowed_values:
            raise ToolArgumentBoundsError(
                f"Parameter '{self.name}': value {value!r} is not in the "
                f"allowed set {sorted(self.allowed_values)}."
            )

        return value


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDefinition:
    r"""Static, immutable description of a registered tool.

    Attributes:
        name:          Canonical tool name (dot-notation, e.g. "fs.read_file").
                       Must match ``[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*``.
        action_name:   Trust Core action label (e.g. "filesystem.read").
                       Used for capability issuance.
        risk:          Pre-classified ActionRisk.  Must be consistent with the
                       Trust Core's canonical action-risk table.
        description:   Human-readable description (shown in approval UIs).
        parameters:    Ordered sequence of parameter schemas.
        scope:         Default ResourceScope.  Callers may narrow this per-call
                       but may NEVER widen it.
        timeout_seconds: Maximum execution time in seconds (default 30).
    """

    name: str
    action_name: str
    risk: ActionRisk
    description: str
    parameters: Sequence[ToolParameterSchema]
    scope: ResourceScope
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.match(self.name):
            raise ToolRegistrationError(
                f"Tool name {self.name!r} is invalid. "
                "Must match [a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)* ."
            )
        if self.timeout_seconds <= 0:
            raise ToolRegistrationError(f"Tool '{self.name}': timeout_seconds must be positive.")

    @property
    def parameter_map(self) -> Dict[str, ToolParameterSchema]:
        """Return parameters keyed by name for fast lookup."""
        return {p.name: p for p in self.parameters}

    def validate_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and coerce *args* against the declared parameter schema.

        Returns a new dict with coerced values.

        Raises:
            ToolArgumentMissingError: A required parameter is absent.
            ToolArgumentTypeError:    A parameter has the wrong type.
            ToolArgumentBoundsError:  A parameter value is out of bounds.
        """
        pmap = self.parameter_map
        result: Dict[str, Any] = {}

        # Check required params
        for param in self.parameters:
            if param.required and param.name not in args:
                raise ToolArgumentMissingError(
                    f"Tool '{self.name}': required parameter '{param.name}' is missing."
                )

        # Validate provided params
        for key, raw_value in args.items():
            if key not in pmap:
                # Unknown parameters are silently ignored (fail-safe: don't error
                # on extra context keys, but don't pass them to execution either)
                continue
            result[key] = pmap[key].validate(raw_value)

        # Add defaults for optional params not provided
        for param in self.parameters:
            if not param.required and param.name not in result:
                result[param.name] = None

        return result


# ---------------------------------------------------------------------------
# Tool result
# ---------------------------------------------------------------------------


@unique
class ToolResultStatus(str, Enum):
    """Outcome of a tool execution."""

    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ToolResult:
    """Uniform return type from all Tool Broker executions.

    Attributes:
        tool_name:    Name of the tool that was invoked.
        status:       Outcome status.
        output:       Sanitized output text (empty string on error/denied).
        error_type:   Exception class name on failure; None on success.
        error_summary: Short human-readable failure description (no secrets).
        exit_code:    Process exit code for subprocess tools; None otherwise.
    """

    tool_name: str
    status: ToolResultStatus
    output: str
    error_type: Optional[str] = None
    error_summary: Optional[str] = None
    exit_code: Optional[int] = None

    @classmethod
    def success(cls, tool_name: str, output: str, exit_code: Optional[int] = None) -> "ToolResult":
        """Convenience constructor for a successful result."""
        return cls(
            tool_name=tool_name,
            status=ToolResultStatus.SUCCESS,
            output=output,
            exit_code=exit_code,
        )

    @classmethod
    def error(
        cls,
        tool_name: str,
        error_type: str,
        error_summary: str,
        exit_code: Optional[int] = None,
    ) -> "ToolResult":
        """Convenience constructor for an error result."""
        return cls(
            tool_name=tool_name,
            status=ToolResultStatus.ERROR,
            output="",
            error_type=error_type,
            error_summary=error_summary,
            exit_code=exit_code,
        )

    @classmethod
    def denied(cls, tool_name: str, error_summary: str) -> "ToolResult":
        """Convenience constructor for an authorization-denied result."""
        return cls(
            tool_name=tool_name,
            status=ToolResultStatus.DENIED,
            output="",
            error_type="ToolAuthorizationError",
            error_summary=error_summary,
        )

    @classmethod
    def timeout(cls, tool_name: str, timeout_seconds: float) -> "ToolResult":
        """Convenience constructor for a timeout result."""
        return cls(
            tool_name=tool_name,
            status=ToolResultStatus.TIMEOUT,
            output="",
            error_type="ToolTimeoutError",
            error_summary=f"Tool exceeded {timeout_seconds}s timeout.",
        )
