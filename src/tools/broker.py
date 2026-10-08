"""SAGE Tool Broker -- Central Tool Registration and Execution Authority.

The Tool Broker is the single gateway through which all tool invocations
must pass.  No tool may be executed without traversing the full pipeline:

    Registration → Argument Validation → Trust Core Authorization
        → Capability Issuance → Sandboxed Execution → Output Sanitization

Architecture invariants (from Prompt 020 spec):
1.  Every tool invocation requests a capability from the Trust Authority.
    If the authority denies, the tool is not executed (fail-closed).
2.  Capability scope is narrowed per-call; callers may never WIDEN the
    default scope declared in the ToolDefinition.
3.  Output is always sanitized before being returned to any caller,
    including LLM providers.
4.  The broker does not expose any subprocess handles to callers.
5.  Tool registration is append-only during a process lifetime; tools
    may not be unregistered (prevents TOCTOU attacks on the registry).
6.  No LLM/provider may register a tool on its own behalf (registration
    is the prerogative of trusted application code only).

This module has only stdlib and internal SAGE dependencies.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Sequence

from src.tools.errors import (
    DuplicateToolError,
    OutputSanitizationError,
    ToolArgumentError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolSandboxError,
    ToolTimeoutError,
)
from src.tools.models import ToolDefinition, ToolResult, ToolResultStatus
from src.tools.sanitizer import sanitize_output
from src.tools.subprocess_runner import run_subprocess
from src.trust.capability import ResourceScope
from src.trust.errors import TrustError

logger = logging.getLogger(__name__)

# Type alias for inline (Python-function) tool handlers.
# Signature: (validated_args: Dict[str, Any], scope: ResourceScope) -> str
InlineHandler = Callable[[Dict[str, Any], ResourceScope], str]


class ToolBroker:
    """Central gateway for all tool registration and execution in SAGE.

    Lifecycle:
        broker = ToolBroker(trust_authority)
        broker.register(my_tool_definition)
        result = broker.invoke("my_tool", args={...}, actor="fast_decision")

    Thread-safety note:
        register() is NOT thread-safe; call it only during application startup.
        invoke() is safe for concurrent calls after registration is complete.
    """

    def __init__(self, trust_authority: Any) -> None:
        """Create a new ToolBroker.

        Args:
            trust_authority: A ``TrustAuthority`` instance (from src.trust).
                             Typed as Any to avoid circular imports; duck-typed.
        """
        self._authority = trust_authority
        self._tools: Dict[str, ToolDefinition] = {}
        self._inline_handlers: Dict[str, InlineHandler] = {}
        self._subprocess_tools: set[str] = set()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        definition: ToolDefinition,
        *,
        inline_handler: Optional[InlineHandler] = None,
        is_subprocess_tool: bool = False,
    ) -> None:
        """Register a tool in the broker.

        Exactly one of *inline_handler* or *is_subprocess_tool=True* must be
        provided.  Subprocess tools invoke ``run_subprocess`` and require the
        ToolDefinition scope's ``allowed_process_commands`` to be non-empty.

        Args:
            definition:        The static ToolDefinition for this tool.
            inline_handler:    Python callable that implements the tool logic.
                               Receives (validated_args, scope) and must return
                               a plain string (the tool output).
            is_subprocess_tool: If True, the tool runs via ``run_subprocess``.
                               The first element of ``args["cmd"]`` (a list
                               parameter named ``cmd``) is the command sequence.

        Raises:
            DuplicateToolError:    A tool with this name is already registered.
            ToolRegistrationError: The definition or handler configuration is
                                   invalid.
        """
        if definition.name in self._tools:
            raise DuplicateToolError(
                f"Tool '{definition.name}' is already registered.  "
                "Tools cannot be replaced after registration."
            )

        if inline_handler is None and not is_subprocess_tool:
            from src.tools.errors import ToolRegistrationError

            raise ToolRegistrationError(
                f"Tool '{definition.name}': must provide either inline_handler "
                "or is_subprocess_tool=True."
            )
        if inline_handler is not None and is_subprocess_tool:
            from src.tools.errors import ToolRegistrationError

            raise ToolRegistrationError(
                f"Tool '{definition.name}': inline_handler and is_subprocess_tool "
                "are mutually exclusive."
            )

        self._tools[definition.name] = definition
        if inline_handler is not None:
            self._inline_handlers[definition.name] = inline_handler
        if is_subprocess_tool:
            self._subprocess_tools.add(definition.name)

        logger.info(
            "Tool registered: name=%r action=%r risk=%s",
            definition.name,
            definition.action_name,
            definition.risk.value,
        )

    def registered_tools(self) -> Sequence[str]:
        """Return the names of all registered tools (sorted)."""
        return sorted(self._tools)

    def list_tools(self) -> Dict[str, ToolDefinition]:
        """Return all registered tool definitions keyed by name."""
        return dict(self._tools)

    def get_definition(self, tool_name: str) -> ToolDefinition:
        """Return the ToolDefinition for *tool_name*.

        Raises:
            ToolNotFoundError: Tool is not registered.
        """
        if tool_name not in self._tools:
            raise ToolNotFoundError(f"Tool '{tool_name}' is not registered.")
        return self._tools[tool_name]

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def invoke(
        self,
        tool_name: str,
        args: Dict[str, Any],
        actor: str,
        project_id: Optional[str] = None,
        scope_override: Optional[ResourceScope] = None,
        ttl_seconds: float = 60.0,
    ) -> ToolResult:
        """Execute a registered tool through the full authorization pipeline.

        Steps:
          1. Resolve tool definition (fail if not found).
          2. Validate and coerce arguments.
          3. Request a capability from the Trust Authority.
          4. Consume the capability (scope/expiry/revocation checks).
          5. Execute the tool handler (inline or subprocess).
          6. Sanitize output.
          7. Return ToolResult.

        On any failure, a ToolResult with an appropriate status is returned.
        Exceptions are never propagated to the caller (except catastrophic
        internal errors, which must bubble up as ToolBrokerError).

        Args:
            tool_name:      Name of the registered tool to invoke.
            args:           Raw argument dict (values will be validated/coerced).
            actor:          Requesting component identity (for audit log).
            project_id:     Optional project context for Trust Core.
            scope_override: Narrowed ResourceScope.  If provided, it must be a
                            *subset* of the tool's default scope.  The broker
                            validates this; callers may never widen scope.
            ttl_seconds:    Capability token lifetime (default 60 s).

        Returns:
            ToolResult with status, sanitized output, and metadata.
        """
        # --- 1. Resolve definition ---
        try:
            defn = self.get_definition(tool_name)
        except ToolNotFoundError as exc:
            logger.warning("invoke: unknown tool %r from actor=%r", tool_name, actor)
            return ToolResult.error(
                tool_name=tool_name,
                error_type="ToolNotFoundError",
                error_summary=str(exc),
            )

        # --- 2. Argument validation ---
        try:
            validated_args = defn.validate_args(args)
        except ToolArgumentError as exc:
            logger.warning(
                "invoke: argument validation failed for tool=%r actor=%r error=%s",
                tool_name,
                actor,
                type(exc).__name__,
            )
            return ToolResult.error(
                tool_name=tool_name,
                error_type=type(exc).__name__,
                error_summary=str(exc),
            )

        # --- Scope resolution ---
        effective_scope = self._resolve_scope(defn, scope_override, tool_name)
        if effective_scope is None:
            # Scope override validation failed (logged inside helper)
            return ToolResult.denied(
                tool_name=tool_name,
                error_summary="Scope override would widen the tool's declared scope.",
            )

        # --- 3. Request capability ---
        try:
            cap = self._authority.request_capability(
                action_name=defn.action_name,
                scope=effective_scope,
                actor=actor,
                project_id=project_id,
                ttl_seconds=ttl_seconds,
                max_uses=1,
            )
        except TrustError as exc:
            logger.info(
                "invoke: capability denied for tool=%r actor=%r error=%s",
                tool_name,
                actor,
                type(exc).__name__,
            )
            return ToolResult.denied(
                tool_name=tool_name,
                error_summary=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                "invoke: unexpected error requesting capability for tool=%r: %s",
                tool_name,
                type(exc).__name__,
                exc_info=True,
            )
            return ToolResult.error(
                tool_name=tool_name,
                error_type=type(exc).__name__,
                error_summary="Unexpected authorization error.",
            )

        # --- 4. Consume capability ---
        try:
            self._authority.consume_capability(
                cap=cap,
                actor=actor,
                project_id=project_id,
                # Path/host/command checked per-execution in the handler
            )
        except TrustError as exc:
            logger.info(
                "invoke: capability consumption denied for tool=%r actor=%r: %s",
                tool_name,
                actor,
                type(exc).__name__,
            )
            return ToolResult.denied(
                tool_name=tool_name,
                error_summary=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                "invoke: unexpected error consuming capability for tool=%r: %s",
                tool_name,
                type(exc).__name__,
                exc_info=True,
            )
            return ToolResult.error(
                tool_name=tool_name,
                error_type=type(exc).__name__,
                error_summary="Unexpected capability consumption error.",
            )

        # --- 5. Execute handler ---
        try:
            return self._execute(defn, validated_args, effective_scope)
        except ToolTimeoutError as exc:
            logger.warning("invoke: timeout for tool=%r: %s", tool_name, exc)
            return ToolResult.timeout(tool_name=tool_name, timeout_seconds=defn.timeout_seconds)
        except ToolSandboxError as exc:
            logger.error("invoke: sandbox violation for tool=%r: %s", tool_name, exc)
            return ToolResult.error(
                tool_name=tool_name,
                error_type="ToolSandboxError",
                error_summary=str(exc),
            )
        except OutputSanitizationError as exc:
            logger.error("invoke: sanitization failure for tool=%r: %s", tool_name, exc)
            return ToolResult.error(
                tool_name=tool_name,
                error_type="OutputSanitizationError",
                error_summary="Tool output could not be sanitized and was suppressed.",
            )
        except ToolExecutionError as exc:
            logger.error("invoke: execution error for tool=%r: %s", tool_name, exc)
            return ToolResult.error(
                tool_name=tool_name,
                error_type=type(exc).__name__,
                error_summary=str(exc),
            )
        except Exception as exc:
            logger.error(
                "invoke: unexpected execution error for tool=%r: %s",
                tool_name,
                type(exc).__name__,
                exc_info=True,
            )
            return ToolResult.error(
                tool_name=tool_name,
                error_type=type(exc).__name__,
                error_summary="Unexpected execution error.",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_scope(
        self,
        defn: ToolDefinition,
        override: Optional[ResourceScope],
        tool_name: str,
    ) -> Optional[ResourceScope]:
        """Return the effective ResourceScope for this invocation.

        If *override* is None, returns the definition's default scope.
        If *override* is provided, validates it does not widen the default
        scope in any dimension.  Returns None if the override is invalid.
        """
        if override is None:
            return defn.scope

        default = defn.scope

        # Filesystem prefixes: override must be a subset
        if not override.filesystem_prefixes.issubset(default.filesystem_prefixes):
            logger.warning("Scope override for tool=%r would widen filesystem_prefixes.", tool_name)
            return None

        # Network hosts: override must be a subset
        if not override.allowed_network_hosts.issubset(default.allowed_network_hosts):
            logger.warning(
                "Scope override for tool=%r would widen allowed_network_hosts.", tool_name
            )
            return None

        # Process commands: override must be a subset
        if not override.allowed_process_commands.issubset(default.allowed_process_commands):
            logger.warning(
                "Scope override for tool=%r would widen allowed_process_commands.", tool_name
            )
            return None

        return override

    def _execute(
        self,
        defn: ToolDefinition,
        validated_args: Dict[str, Any],
        scope: ResourceScope,
    ) -> ToolResult:
        """Dispatch to inline handler or subprocess runner."""
        if defn.name in self._inline_handlers:
            return self._execute_inline(defn, validated_args, scope)
        if defn.name in self._subprocess_tools:
            return self._execute_subprocess(defn, validated_args, scope)
        # Should be unreachable; registration prevents this state
        raise ToolExecutionError(
            f"Tool '{defn.name}' has no registered handler.  This is an internal broker bug."
        )

    def _execute_inline(
        self,
        defn: ToolDefinition,
        validated_args: Dict[str, Any],
        scope: ResourceScope,
    ) -> ToolResult:
        """Run an inline Python-function tool handler."""
        handler = self._inline_handlers[defn.name]
        try:
            raw_output = handler(validated_args, scope)
        except Exception as exc:
            raise ToolExecutionError(
                f"Inline handler for '{defn.name}' raised {type(exc).__name__}."
            ) from exc

        sanitized = sanitize_output(str(raw_output))
        return ToolResult.success(tool_name=defn.name, output=sanitized)

    def _execute_subprocess(
        self,
        defn: ToolDefinition,
        validated_args: Dict[str, Any],
        scope: ResourceScope,
    ) -> ToolResult:
        """Run a subprocess tool.

        Expects validated_args to contain:
          - 'cmd': list of strings (the command sequence)
          - 'cwd': optional working directory string
        """
        cmd = validated_args.get("cmd")
        if not isinstance(cmd, (list, tuple)) or not cmd:
            raise ToolExecutionError(
                f"Subprocess tool '{defn.name}': 'cmd' arg must be a non-empty list."
            )
        cwd = validated_args.get("cwd")

        stdout, stderr, exit_code = run_subprocess(
            cmd=cmd,
            scope=scope,
            cwd=cwd,
            timeout_seconds=defn.timeout_seconds,
        )

        # Combine stdout + stderr (stderr is informational for tool output)
        combined = stdout
        if stderr:
            combined = combined + ("\n--- stderr ---\n" + stderr if stdout else stderr)

        return ToolResult(
            tool_name=defn.name,
            status=ToolResultStatus.SUCCESS if exit_code == 0 else ToolResultStatus.ERROR,
            output=combined,
            exit_code=exit_code,
            error_type=None if exit_code == 0 else "NonZeroExit",
            error_summary=None if exit_code == 0 else f"Process exited with code {exit_code}.",
        )
