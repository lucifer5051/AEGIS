"""Tests for the SAGE Tool Broker (Prompt 020, Parts E–I).

Coverage:
- ToolParameterSchema validation (types, bounds, required, allowed_values)
- ToolDefinition name validation and validate_args
- ToolResult constructors
- Output sanitizer (ANSI stripping, secret redaction, length limits)
- ToolBroker registration (inline, subprocess, duplicate, bad config)
- ToolBroker invocation (allow, denied, argument error, not found, timeout)
- Scope widening prevention
- Subprocess runner sandbox checks (without actually spawning unsafe processes)
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

import pytest

from src.tools import (
    DuplicateToolError,
    ParamType,
    ToolArgumentBoundsError,
    ToolArgumentMissingError,
    ToolArgumentTypeError,
    ToolBroker,
    ToolDefinition,
    ToolNotFoundError,
    ToolParameterSchema,
    ToolResult,
    ToolResultStatus,
    sanitize_output,
)
from src.tools.errors import ToolRegistrationError, ToolSandboxError
from src.trust import AutonomyTier, build_trust_authority
from src.trust.capability import ActionRisk, ResourceScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_authority(tier: AutonomyTier = AutonomyTier.FULL_BOUNDED):
    """Create a real TrustAuthority backed by an in-memory DB.

    Uses the exact audit_records schema from migration 0005_trust_core_schema.sql.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_records (
            audit_id            TEXT PRIMARY KEY,
            event_type          TEXT NOT NULL,
            actor               TEXT NOT NULL,
            target_entity_type  TEXT NOT NULL,
            target_entity_id    TEXT NOT NULL,
            project_id          TEXT NULL,
            decision            TEXT NOT NULL,
            policy_rule_id      TEXT NOT NULL,
            telemetry_json      TEXT NOT NULL,
            timestamp           TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return build_trust_authority(conn, autonomy_tier=tier)


def _simple_tool(
    name: str = "test.echo",
    action_name: str = "filesystem.read",
    risk: ActionRisk = ActionRisk.LOW,
    params=None,
    scope=None,
) -> ToolDefinition:
    if params is None:
        params = [ToolParameterSchema(name="message", param_type=ParamType.STRING)]
    if scope is None:
        scope = ResourceScope(filesystem_prefixes=frozenset({"/tmp"}))
    return ToolDefinition(
        name=name,
        action_name=action_name,
        risk=risk,
        description="A test echo tool.",
        parameters=params,
        scope=scope,
    )


def _echo_handler(args: Dict[str, Any], scope: ResourceScope) -> str:
    return str(args.get("message", ""))


# ---------------------------------------------------------------------------
# ToolParameterSchema tests
# ---------------------------------------------------------------------------


class TestToolParameterSchema:
    def test_string_coercion(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.STRING)
        assert p.validate(42) == "42"

    def test_integer_coercion(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.INTEGER)
        assert p.validate("7") == 7

    def test_float_coercion(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.FLOAT)
        assert abs(p.validate("3.14") - 3.14) < 1e-9

    def test_boolean_true_variants(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.BOOLEAN)
        for v in (True, "true", "True", "1", "yes", 1):
            assert p.validate(v) is True

    def test_boolean_false_variants(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.BOOLEAN)
        for v in (False, "false", "False", "0", "no", 0):
            assert p.validate(v) is False

    def test_integer_min_bound(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.INTEGER, min_value=0)
        with pytest.raises(ToolArgumentBoundsError):
            p.validate(-1)

    def test_integer_max_bound(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.INTEGER, max_value=10)
        with pytest.raises(ToolArgumentBoundsError):
            p.validate(11)

    def test_string_max_length(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.STRING, max_length=5)
        with pytest.raises(ToolArgumentBoundsError):
            p.validate("toolong")

    def test_allowed_values_pass(self):
        p = ToolParameterSchema(
            name="x",
            param_type=ParamType.STRING,
            allowed_values=frozenset({"a", "b", "c"}),
        )
        assert p.validate("a") == "a"

    def test_allowed_values_fail(self):
        p = ToolParameterSchema(
            name="x",
            param_type=ParamType.STRING,
            allowed_values=frozenset({"a", "b"}),
        )
        with pytest.raises(ToolArgumentBoundsError):
            p.validate("z")

    def test_bad_type_raises(self):
        p = ToolParameterSchema(name="x", param_type=ParamType.INTEGER)
        with pytest.raises(ToolArgumentTypeError):
            p.validate("not_a_number")


# ---------------------------------------------------------------------------
# ToolDefinition tests
# ---------------------------------------------------------------------------


class TestToolDefinition:
    def test_invalid_name_raises(self):
        with pytest.raises(ToolRegistrationError):
            ToolDefinition(
                name="Bad Name!",
                action_name="filesystem.read",
                risk=ActionRisk.LOW,
                description="",
                parameters=[],
                scope=ResourceScope(),
            )

    def test_valid_dotted_name(self):
        defn = _simple_tool(name="fs.read_file")
        assert defn.name == "fs.read_file"

    def test_validate_args_missing_required(self):
        defn = _simple_tool()
        with pytest.raises(ToolArgumentMissingError):
            defn.validate_args({})

    def test_validate_args_unknown_key_ignored(self):
        defn = _simple_tool()
        result = defn.validate_args({"message": "hello", "unknown_key": "ignored"})
        assert "message" in result
        assert "unknown_key" not in result

    def test_validate_args_optional_absent(self):
        params = [
            ToolParameterSchema(name="required", param_type=ParamType.STRING, required=True),
            ToolParameterSchema(name="optional", param_type=ParamType.STRING, required=False),
        ]
        defn = _simple_tool(params=params)
        result = defn.validate_args({"required": "hi"})
        assert result["required"] == "hi"
        assert result["optional"] is None

    def test_zero_timeout_raises(self):
        with pytest.raises(ToolRegistrationError):
            ToolDefinition(
                name="test.bad",
                action_name="filesystem.read",
                risk=ActionRisk.LOW,
                description="",
                parameters=[],
                scope=ResourceScope(),
                timeout_seconds=0,
            )


# ---------------------------------------------------------------------------
# ToolResult tests
# ---------------------------------------------------------------------------


class TestToolResult:
    def test_success_constructor(self):
        r = ToolResult.success("my.tool", "hello")
        assert r.status == ToolResultStatus.SUCCESS
        assert r.output == "hello"
        assert r.error_type is None

    def test_error_constructor(self):
        r = ToolResult.error("my.tool", "SomeError", "A description")
        assert r.status == ToolResultStatus.ERROR
        assert r.output == ""
        assert r.error_type == "SomeError"

    def test_denied_constructor(self):
        r = ToolResult.denied("my.tool", "PolicyDeniedError")
        assert r.status == ToolResultStatus.DENIED

    def test_timeout_constructor(self):
        r = ToolResult.timeout("my.tool", 30.0)
        assert r.status == ToolResultStatus.TIMEOUT
        assert "30" in (r.error_summary or "")


# ---------------------------------------------------------------------------
# Sanitizer tests
# ---------------------------------------------------------------------------


class TestSanitizeOutput:
    def test_ansi_stripped(self):
        raw = "\x1b[31mred text\x1b[0m"
        assert sanitize_output(raw) == "red text"

    def test_aws_key_redacted(self):
        raw = "Found key: AKIAIOSFODNN7EXAMPLE in config"
        out = sanitize_output(raw)
        assert "AKIA" not in out
        assert "[REDACTED]" in out

    def test_bearer_token_redacted(self):
        raw = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc123def456"
        out = sanitize_output(raw)
        assert "eyJhbGci" not in out

    def test_pem_key_redacted(self):
        raw = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
        out = sanitize_output(raw)
        assert "MIIEpAIBAAKCAQEA" not in out
        assert "[REDACTED]" in out

    def test_github_token_redacted(self):
        raw = "Token: ghp_" + "A" * 36
        out = sanitize_output(raw)
        assert "ghp_" not in out

    def test_password_assignment_redacted(self):
        raw = "password=supersecret123"
        out = sanitize_output(raw)
        assert "supersecret123" not in out

    def test_truncation_by_lines(self):
        raw = "\n".join(["line"] * 3000)
        out = sanitize_output(raw)
        assert "truncated" in out

    def test_normal_text_unchanged(self):
        raw = "Hello, World! This is safe text."
        assert sanitize_output(raw) == raw


# ---------------------------------------------------------------------------
# ToolBroker registration tests
# ---------------------------------------------------------------------------


class TestToolBrokerRegistration:
    def _broker(self):
        return ToolBroker(_make_authority())

    def test_register_inline(self):
        broker = self._broker()
        broker.register(_simple_tool(), inline_handler=_echo_handler)
        assert "test.echo" in broker.registered_tools()

    def test_register_duplicate_raises(self):
        broker = self._broker()
        broker.register(_simple_tool(), inline_handler=_echo_handler)
        with pytest.raises(DuplicateToolError):
            broker.register(_simple_tool(), inline_handler=_echo_handler)

    def test_register_no_handler_raises(self):
        broker = self._broker()
        with pytest.raises(ToolRegistrationError):
            broker.register(_simple_tool())

    def test_register_both_handlers_raises(self):
        broker = self._broker()
        with pytest.raises(ToolRegistrationError):
            broker.register(
                _simple_tool(),
                inline_handler=_echo_handler,
                is_subprocess_tool=True,
            )

    def test_get_definition(self):
        broker = self._broker()
        defn = _simple_tool()
        broker.register(defn, inline_handler=_echo_handler)
        assert broker.get_definition("test.echo") is defn

    def test_get_definition_not_found(self):
        broker = self._broker()
        with pytest.raises(ToolNotFoundError):
            broker.get_definition("nonexistent")


# ---------------------------------------------------------------------------
# ToolBroker invocation tests
# ---------------------------------------------------------------------------


class TestToolBrokerInvoke:
    def _broker_with_echo(self, tier=AutonomyTier.FULL_BOUNDED):
        authority = _make_authority(tier)
        broker = ToolBroker(authority)
        broker.register(_simple_tool(), inline_handler=_echo_handler)
        return broker

    def test_successful_invocation(self):
        broker = self._broker_with_echo()
        result = broker.invoke("test.echo", args={"message": "hello"}, actor="test")
        assert result.status == ToolResultStatus.SUCCESS
        assert result.output == "hello"

    def test_unknown_tool_returns_error(self):
        broker = self._broker_with_echo()
        result = broker.invoke("nonexistent", args={}, actor="test")
        assert result.status == ToolResultStatus.ERROR
        assert result.error_type == "ToolNotFoundError"

    def test_missing_required_arg_returns_error(self):
        broker = self._broker_with_echo()
        result = broker.invoke("test.echo", args={}, actor="test")
        assert result.status == ToolResultStatus.ERROR
        assert "ToolArgumentMissing" in (result.error_type or "")

    def test_denied_by_policy(self):
        # STRICT_MANUAL only allows LOW risk autonomously; filesystem.read IS LOW
        # so use a CRITICAL action to force denial escalation
        authority = _make_authority(AutonomyTier.STRICT_MANUAL)
        broker = ToolBroker(authority)
        # Register a HIGH risk tool — will require escalation at STRICT_MANUAL
        high_tool = _simple_tool(
            name="test.dangerous",
            action_name="filesystem.delete",
            risk=ActionRisk.HIGH,
        )
        broker.register(high_tool, inline_handler=_echo_handler)
        result = broker.invoke("test.dangerous", args={"message": "x"}, actor="test")
        # STRICT_MANUAL will escalate HIGH risk -> ApprovalRequiredError -> DENIED
        assert result.status == ToolResultStatus.DENIED

    def test_scope_override_widening_rejected(self):
        authority = _make_authority()
        broker = ToolBroker(authority)
        # Tool's default scope: /tmp only
        broker.register(_simple_tool(), inline_handler=_echo_handler)
        # Override tries to widen to /etc — must be rejected
        wider_scope = ResourceScope(filesystem_prefixes=frozenset({"/tmp", "/etc"}))
        result = broker.invoke(
            "test.echo",
            args={"message": "x"},
            actor="test",
            scope_override=wider_scope,
        )
        assert result.status == ToolResultStatus.DENIED

    def test_scope_override_narrowing_allowed(self):
        authority = _make_authority()
        broker = ToolBroker(authority)
        # Tool default scope: /tmp; override narrows (empty = no filesystem access)
        broker.register(_simple_tool(), inline_handler=_echo_handler)
        narrower_scope = ResourceScope(filesystem_prefixes=frozenset())
        # Narrower is subset of {"/tmp"}, so it's allowed
        result = broker.invoke(
            "test.echo",
            args={"message": "hi"},
            actor="test",
            scope_override=narrower_scope,
        )
        assert result.status == ToolResultStatus.SUCCESS

    def test_output_sanitized(self):
        """Handler that returns a secret pattern must have it redacted."""
        authority = _make_authority()
        broker = ToolBroker(authority)
        broker.register(
            _simple_tool(),
            inline_handler=lambda args, scope: "password=topsecret999",
        )
        result = broker.invoke("test.echo", args={"message": "x"}, actor="test")
        assert result.status == ToolResultStatus.SUCCESS
        assert "topsecret999" not in result.output
        assert "[REDACTED]" in result.output

    def test_handler_exception_returns_error(self):
        authority = _make_authority()
        broker = ToolBroker(authority)
        broker.register(
            _simple_tool(),
            inline_handler=lambda args, scope: (_ for _ in ()).throw(
                ValueError("something went wrong")
            ),
        )
        result = broker.invoke("test.echo", args={"message": "x"}, actor="test")
        assert result.status == ToolResultStatus.ERROR


# ---------------------------------------------------------------------------
# Subprocess runner sandbox tests
# ---------------------------------------------------------------------------


class TestSubprocessRunner:
    def test_command_not_in_allowlist_raises(self):
        from src.tools.subprocess_runner import run_subprocess

        scope = ResourceScope(
            filesystem_prefixes=frozenset({"/tmp"}),
            allowed_process_commands=frozenset({"echo"}),
        )
        with pytest.raises(ToolSandboxError, match="allowlist"):
            run_subprocess(["python", "-c", "print('hi')"], scope=scope, cwd=None)

    def test_empty_command_raises(self):
        from src.tools.subprocess_runner import run_subprocess

        scope = ResourceScope()
        with pytest.raises(ToolSandboxError):
            run_subprocess([], scope=scope)

    def test_allowed_command_runs(self):
        """Run a real no-op subprocess that should succeed."""
        import os
        import sys

        from src.tools.subprocess_runner import run_subprocess

        # Use python itself since it's guaranteed to exist
        python_exe = sys.executable
        python_basename = os.path.basename(python_exe)
        # Normalise to handle python.exe vs python3 on different platforms
        scope = ResourceScope(
            filesystem_prefixes=frozenset({os.path.dirname(python_exe), "/", "C:\\"}),
            allowed_process_commands=frozenset({python_basename}),
        )
        stdout, stderr, code = run_subprocess(
            [python_exe, "-c", "print('ok')"],
            scope=scope,
        )
        assert code == 0
        assert "ok" in stdout
