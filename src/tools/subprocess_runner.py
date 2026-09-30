"""SAGE Tool Broker -- Sandboxed Subprocess Execution.

Provides a safe, opinionated wrapper around subprocess execution with:
- Explicit allowlist of permitted command basenames (from ResourceScope).
- Timeout enforcement (raises ToolTimeoutError).
- Stdout/stderr capture with sanitized output.
- Working directory restriction to declared filesystem prefixes.
- No shell=True (prevents shell injection).
- Environment stripping: only a minimal, curated environment is passed to
  child processes (no credential forwarding).

IMPORTANT: This module does NOT provide OS-level sandboxing (e.g., seccomp,
AppArmor, containers, or Windows job objects).  It is a process-level guard
only.  Do NOT claim OS-level isolation is provided.

This module has ZERO external dependencies beyond stdlib.
"""

import os
import subprocess
from typing import Dict, Optional, Sequence

from src.tools.errors import ToolSandboxError, ToolTimeoutError
from src.tools.sanitizer import sanitize_output
from src.trust.capability import ResourceScope

# ---------------------------------------------------------------------------
# Minimal environment for subprocess execution
# ---------------------------------------------------------------------------

# Keys forwarded from the parent environment to child processes.
# Sensitive keys (credentials, tokens, etc.) are intentionally excluded.
_FORWARDED_ENV_KEYS: frozenset = frozenset(
    {
        "PATH",
        "SYSTEMROOT",  # Windows
        "TEMP",
        "TMP",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PYTHONPATH",  # Required for package imports in tests
        "VIRTUAL_ENV",  # Let subprocess use the venv
        "USERPROFILE",  # Windows home directory
        "APPDATA",  # Windows app data
    }
)


def _build_safe_env() -> Dict[str, str]:
    """Build a minimal environment for child processes.

    Copies only whitelisted keys from os.environ; omits all credentials,
    tokens, and other sensitive keys.
    """
    parent = os.environ
    return {k: v for k, v in parent.items() if k in _FORWARDED_ENV_KEYS}


# ---------------------------------------------------------------------------
# Public subprocess runner
# ---------------------------------------------------------------------------


def run_subprocess(
    cmd: Sequence[str],
    scope: ResourceScope,
    cwd: Optional[str] = None,
    timeout_seconds: float = 30.0,
    extra_env: Optional[Dict[str, str]] = None,
) -> tuple[str, str, int]:
    """Execute *cmd* as a subprocess with sandbox constraints applied.

    Args:
        cmd:             Command sequence (no shell expansion).  The first
                         element must be the executable name or path.
        scope:           ResourceScope from the issued capability.  Used to
                         validate command and working directory.
        cwd:             Working directory for the subprocess.  Must be within
                         an allowed filesystem prefix in *scope*.
        timeout_seconds: Maximum execution time in seconds.
        extra_env:       Additional environment variables to add to the minimal
                         safe environment (e.g. tool-specific overrides).

    Returns:
        (sanitized_stdout, sanitized_stderr, exit_code) tuple.

    Raises:
        ToolSandboxError:  Command is not in the scope allowlist, or *cwd* is
                           outside declared filesystem prefixes.
        ToolTimeoutError:  Process did not complete within *timeout_seconds*.
    """
    if not cmd:
        raise ToolSandboxError("Command sequence is empty.")

    executable = cmd[0]

    # --- Command allowlist check ---
    if not scope.contains_command(executable):
        raise ToolSandboxError(
            f"Command {os.path.basename(executable)!r} is not in the capability scope allowlist."
        )

    # --- Working directory check ---
    if cwd is not None:
        norm_cwd = os.path.realpath(cwd)
        if not scope.contains_path(norm_cwd):
            raise ToolSandboxError("Working directory is outside the declared filesystem scope.")
    else:
        norm_cwd = None

    # --- Build environment ---
    env = _build_safe_env()
    if extra_env:
        # Only allow string-typed extra env values (no injection via bytes)
        for k, v in extra_env.items():
            if isinstance(k, str) and isinstance(v, str):
                env[k] = v

    # --- Execute ---
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=norm_cwd,
            env=env,
            shell=False,  # NEVER use shell=True
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeoutError(
            f"Process {os.path.basename(executable)!r} exceeded {timeout_seconds}s timeout."
        ) from exc
    except FileNotFoundError as exc:
        raise ToolSandboxError(
            f"Executable {os.path.basename(executable)!r} not found on PATH."
        ) from exc
    except PermissionError as exc:
        raise ToolSandboxError(
            f"Permission denied executing {os.path.basename(executable)!r}."
        ) from exc
    except OSError as exc:
        raise ToolSandboxError(
            f"OS error running {os.path.basename(executable)!r}: {type(exc).__name__}"
        ) from exc

    stdout = sanitize_output(result.stdout or "")
    stderr = sanitize_output(result.stderr or "")
    return stdout, stderr, result.returncode
