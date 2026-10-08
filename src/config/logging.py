"""SAGE Logging Configuration -- Safe, Rotated, Zero-Secret Application Logging.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md

Invariants:
1. Log destination defaults to %USERPROFILE%\\.aegis\\logs\\sage.log (outside OneDrive).
2. RotatingFileHandler with 10 MB per file, max 5 backup copies.
3. Strict zero-secret filter: redacts tokens, passwords, keys, and private credentials.
4. Independent from Trust Core audit log (which writes append-only to SQLite audit_records).
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from src.storage.paths import get_default_logs_dir

# Secret-redaction regex patterns
_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[A-Za-z0-9\-_]{8,}", re.IGNORECASE),
    re.compile(r"(?i)(password\s*[=:]\s*)[^\s,;]{4,}", re.IGNORECASE),
    re.compile(r"(?i)(secret\s*[=:]\s*)[A-Za-z0-9\-_]{8,}", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{36,}", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"),
]


class SecretRedactionFilter(logging.Filter):
    """Logging filter that scrubs sensitive patterns from all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.redact(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self.redact(str(a)) for a in record.args)
        return True

    @staticmethod
    def redact(text: str) -> str:
        """Replace matching secret patterns with redaction placeholder."""
        scrubbed = text
        for pattern in _SECRET_PATTERNS:
            scrubbed = pattern.sub("[REDACTED_SECRET]", scrubbed)
        return scrubbed


def setup_sage_logging(
    log_level: int = logging.INFO,
    log_file: Optional[Path] = None,
    enable_console: bool = False,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> logging.Logger:
    """Configure and return the root SAGE application logger.

    Args:
        log_level: Standard logging level (e.g. logging.INFO, logging.DEBUG).
        log_file: Optional custom log destination (must reside outside OneDrive).
        enable_console: Whether to attach a StreamHandler for terminal output.
        max_bytes: Rotation size limit in bytes.
        backup_count: Number of rotated log archives to retain.

    Returns:
        Configured logger instance for 'sage'.
    """
    target_path = log_file or (get_default_logs_dir() / "sage.log")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    sage_logger = logging.getLogger("src")
    sage_logger.setLevel(log_level)

    # Avoid duplicate handlers on re-initialization
    if not any(isinstance(h, RotatingFileHandler) for h in sage_logger.handlers):
        formatter = logging.Formatter(
            fmt="%(asctime)s UTC [%(levelname)s] [%(name)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        redaction_filter = SecretRedactionFilter()

        # Rotating file handler
        file_handler = RotatingFileHandler(
            filename=str(target_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction_filter)
        sage_logger.addHandler(file_handler)

        if enable_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(log_level)
            console_handler.setFormatter(formatter)
            console_handler.addFilter(redaction_filter)
            sage_logger.addHandler(console_handler)

    return sage_logger
