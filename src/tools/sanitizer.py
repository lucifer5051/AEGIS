"""SAGE Tool Broker -- Output Sanitization.

Scrubs tool output before it is returned to any caller (LLM or otherwise).

Design goals:
- Remove or redact content that looks like secrets, credentials, or private keys.
- Strip ANSI escape sequences that could cause terminal injection.
- Enforce maximum output length to prevent memory exhaustion.
- Fail-closed: if sanitization raises an unexpected exception, the output is
  suppressed and OutputSanitizationError is raised.

This module has ZERO external dependencies (stdlib only).

IMPORTANT: This module provides *best-effort* scrubbing of obvious secrets.
It does NOT provide a complete security boundary against adversarial output.
Do NOT rely on sanitization alone to prevent secret exfiltration.
"""

import re
from typing import Sequence

from src.tools.errors import OutputSanitizationError

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# Maximum bytes returned as tool output.  Longer output is truncated.
MAX_OUTPUT_BYTES: int = 64 * 1024  # 64 KiB

# Maximum line count returned.
MAX_OUTPUT_LINES: int = 2000

# ---------------------------------------------------------------------------
# Pattern registry for secret detection
# ---------------------------------------------------------------------------

# Patterns are checked against the full output string.  If a match is found,
# the matching group (or the full match) is replaced with the redaction token.
_REDACT_TOKEN = "[REDACTED]"

# Each entry is (description, compiled_pattern, replacement).
# Replacements use back-references where needed.
_SECRET_PATTERNS: Sequence[tuple] = (
    # AWS access keys
    (
        "aws_access_key",
        re.compile(r"(?i)(AKIA[0-9A-Z]{16})"),
        _REDACT_TOKEN,
    ),
    # Generic API key patterns: "api_key=xxx", "api-key: xxx", "apikey: xxx"
    (
        "api_key_assignment",
        re.compile(r'(?i)(api[_\-]?key\s*[=:]\s*)["\']?([A-Za-z0-9\-_\.]{16,})["\']?'),
        r"\1" + _REDACT_TOKEN,
    ),
    # Bearer tokens
    (
        "bearer_token",
        re.compile(r"(?i)(Authorization:\s*Bearer\s+)([A-Za-z0-9\-_\.]{16,})"),
        r"\1" + _REDACT_TOKEN,
    ),
    # Private key blocks (PEM)
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            r".*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        _REDACT_TOKEN,
    ),
    # GitHub tokens (ghp_, gho_, ghs_, ghr_, github_pat_)
    (
        "github_token",
        re.compile(r"(?i)(gh[phosr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})"),
        _REDACT_TOKEN,
    ),
    # Generic password assignments: "password=xxx", "passwd: xxx", "secret=xxx"
    (
        "password_assignment",
        re.compile(r'(?i)((?:password|passwd|secret|token)\s*[=:]\s*)["\']?([^\s"\']{6,})["\']?'),
        r"\1" + _REDACT_TOKEN,
    ),
)

# ---------------------------------------------------------------------------
# ANSI escape sequence stripper
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    """Remove ANSI/VT100 escape sequences from *text*."""
    return _ANSI_ESCAPE_RE.sub("", text)


# ---------------------------------------------------------------------------
# Public sanitization API
# ---------------------------------------------------------------------------


def sanitize_output(raw: str) -> str:
    """Sanitize *raw* tool output before returning it to any caller.

    Steps (in order):
    1. Enforce maximum byte length (truncate with notice).
    2. Enforce maximum line count (truncate with notice).
    3. Strip ANSI escape sequences.
    4. Redact known secret patterns.

    Returns the sanitized string.

    Raises:
        OutputSanitizationError: If sanitization itself raises an unexpected
            exception.  The caller must suppress the output in this case.
    """
    try:
        return _sanitize_internal(raw)
    except OutputSanitizationError:
        raise
    except Exception as exc:
        raise OutputSanitizationError(
            f"Unexpected error during output sanitization: {type(exc).__name__}"
        ) from exc


def _sanitize_internal(raw: str) -> str:
    # 1. Byte length truncation
    encoded = raw.encode("utf-8", errors="replace")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raw = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        raw += f"\n[...output truncated at {MAX_OUTPUT_BYTES} bytes...]"

    # 2. Line count truncation
    lines = raw.splitlines(keepends=True)
    if len(lines) > MAX_OUTPUT_LINES:
        raw = "".join(lines[:MAX_OUTPUT_LINES])
        raw += f"\n[...output truncated at {MAX_OUTPUT_LINES} lines...]"

    # 3. Strip ANSI escapes
    raw = _strip_ansi(raw)

    # 4. Redact secrets
    for _desc, pattern, replacement in _SECRET_PATTERNS:
        raw = pattern.sub(replacement, raw)

    return raw
