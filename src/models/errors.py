"""SAGE Model Gateway -- Typed Error Hierarchy & Secret Redaction.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 13
"""

from __future__ import annotations

import re
from typing import Optional


# Secret pattern matching for redacting API keys and sensitive tokens
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),
    re.compile(r"bearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def redact_secrets_from_text(text: str) -> str:
    """Redact known API keys, tokens, and private keys from error messages or logs."""
    if not text:
        return text
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("[REDACTED_SECRET]", redacted)
    return redacted


class ModelGatewayError(Exception):
    """Base exception for all Model Gateway operations."""

    def __init__(self, message: str, provider_id: Optional[str] = None, model_id: Optional[str] = None) -> None:
        sanitized_msg = redact_secrets_from_text(message)
        super().__init__(sanitized_msg)
        self.message = sanitized_msg
        self.provider_id = provider_id
        self.model_id = model_id


class ModelAuthenticationError(ModelGatewayError):
    """Raised when authentication credentials are invalid or rejected (401)."""


class ModelAuthorizationError(ModelGatewayError):
    """Raised when authorization is denied by provider or TrustCore (403)."""


class ModelRateLimitError(ModelGatewayError):
    """Raised when provider rate limits are exceeded (429)."""

    def __init__(self, message: str, retry_after_seconds: Optional[float] = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class ModelProviderUnavailableError(ModelGatewayError):
    """Raised when provider is offline, unreachable, or 503."""


class ModelTimeoutError(ModelGatewayError):
    """Raised when request times out."""


class ModelContextLengthExceededError(ModelGatewayError):
    """Raised when prompt + max_tokens exceeds model context window."""


class ModelCapabilityUnsupportedError(ModelGatewayError):
    """Raised when a requested capability is not supported by the model."""


class ModelPolicyDeniedError(ModelGatewayError):
    """Raised when security policy or TrustCore forbids model invocation (e.g. cloud in local-only mode)."""


class ModelRoutingError(ModelGatewayError):
    """Raised when router cannot find any compatible, available, and authorized model."""
