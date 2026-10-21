"""SAGE Remote Control Gateway -- Telegram Transport Layer.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- Transport is decoupled from gateway business logic.
- MockTelegramTransport enables 100% offline, deterministic testing.
- Bot token is NEVER leaked in logs, URLs, or exception messages.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.gateway.errors import TransportError
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _redact_token(text: str, token: Optional[str] = None) -> str:
    """Scrub raw Telegram bot tokens and credentials from error text and URLs."""
    if token and token in text:
        text = text.replace(token, "<REDACTED_BOT_TOKEN>")
    # Telegram bot path pattern: /bot<token>/...
    text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<REDACTED_BOT_TOKEN>", text)
    # Generic standalone bot token pattern: digits:alphanumeric_or_underscores
    text = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b", "<REDACTED_BOT_TOKEN>", text)
    return text


class TelegramTransport(ABC):
    """Abstract transport interface for Telegram Bot API communication."""

    @abstractmethod
    def get_updates(
        self,
        offset: Optional[int] = None,
        limit: int = 20,
        timeout: int = 30,
    ) -> List[Dict[str, Any]]:
        """Fetch incoming updates via long polling.

        Args:
            offset: Identifier of the first update to be returned.
            limit: Limits the number of updates to be retrieved (1-100).
            timeout: Timeout in seconds for long polling (0-120).

        Returns:
            List of raw update dictionaries from the provider.
        """

    @abstractmethod
    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a text message to a specified chat.

        Args:
            chat_id: Unique identifier for the target chat.
            text: Text of the message to be sent.
            reply_to_message_id: If the message is a reply, ID of the original message.

        Returns:
            Provider response dictionary containing the sent Message object.
        """


class MockTelegramTransport(TelegramTransport):
    """Deterministic, in-memory fake transport for offline testing."""

    def __init__(self) -> None:
        self._queued_updates: List[Dict[str, Any]] = []
        self.sent_messages: List[Dict[str, Any]] = []
        self._next_sent_id: int = 1000
        self._fail_next_get_updates: Optional[Exception] = None
        self._fail_next_send_message: Optional[Exception] = None

    def enqueue_update(self, update: Dict[str, Any]) -> None:
        """Enqueue a mock raw update from Telegram."""
        self._queued_updates.append(update)

    def set_get_updates_error(self, exc: Exception) -> None:
        """Configure transport to raise an exception on next get_updates call."""
        self._fail_next_get_updates = exc

    def set_send_message_error(self, exc: Exception) -> None:
        """Configure transport to raise an exception on next send_message call."""
        self._fail_next_send_message = exc

    def get_updates(
        self,
        offset: Optional[int] = None,
        limit: int = 20,
        timeout: int = 30,
    ) -> List[Dict[str, Any]]:
        if self._fail_next_get_updates is not None:
            exc = self._fail_next_get_updates
            self._fail_next_get_updates = None
            raise exc

        # Filter by offset if specified
        if offset is not None:
            available = [u for u in self._queued_updates if u.get("update_id", 0) >= offset]
        else:
            available = list(self._queued_updates)

        to_return = available[:limit]
        for item in to_return:
            if item in self._queued_updates:
                self._queued_updates.remove(item)
        return to_return

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        _check_no_secrets(text)
        if self._fail_next_send_message is not None:
            exc = self._fail_next_send_message
            self._fail_next_send_message = None
            raise exc

        msg_id = self._next_sent_id
        self._next_sent_id += 1
        record = {
            "message_id": msg_id,
            "chat": {"id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else str(chat_id)},
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "date": int(time.time()),
        }
        self.sent_messages.append(record)
        return {"ok": True, "result": record}


class HttpTelegramTransport(TelegramTransport):
    """Production HTTP transport communicating with api.telegram.org."""

    BASE_URL = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: str,
        request_timeout: float = 35.0,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
    ) -> None:
        if not bot_token or not bot_token.strip():
            raise TransportError("Bot token cannot be empty.")
        self._token = bot_token
        self._timeout = request_timeout
        self._retry_attempts = retry_attempts
        self._backoff = retry_backoff_seconds

    def _redact(self, text: str) -> str:
        return _redact_token(text, token=self._token)

    def _get_api_url(self, method: str) -> str:
        return f"{self.BASE_URL}/bot{self._token}/{method}"

    def get_updates(
        self,
        offset: Optional[int] = None,
        limit: int = 20,
        timeout: int = 30,
    ) -> List[Dict[str, Any]]:
        import httpx

        params: Dict[str, Any] = {"limit": limit, "timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        url = self._get_api_url("getUpdates")
        last_error = None

        for attempt in range(1, self._retry_attempts + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params)
                    if resp.status_code == 200:
                        data: Dict[str, Any] = resp.json()
                        if data.get("ok"):
                            result: List[Dict[str, Any]] = data.get("result", [])
                            return result
                        desc = self._redact(str(data.get("description", "Unknown error")))
                        raise TransportError(f"Telegram API error: {desc}")
                    desc = self._redact(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    raise TransportError(f"Telegram HTTP failure: {desc}")
            except httpx.RequestError as exc:
                clean_exc = self._redact(str(exc))
                last_error = clean_exc
                logger.warning("Telegram getUpdates attempt %d failed: %s", attempt, clean_exc)
                if attempt < self._retry_attempts:
                    time.sleep(self._backoff * attempt)
            except Exception as exc:
                if isinstance(exc, TransportError):
                    raise
                clean_exc = self._redact(str(exc))
                raise TransportError(f"Unexpected transport error: {clean_exc}") from exc

        raise TransportError(f"getUpdates exhausted {self._retry_attempts} retries: {last_error}")

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        _check_no_secrets(text)
        import httpx

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        url = self._get_api_url("sendMessage")
        last_error = None

        for attempt in range(1, self._retry_attempts + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(url, json=payload)
                    if resp.status_code == 200:
                        resp_data: Dict[str, Any] = resp.json()
                        if resp_data.get("ok"):
                            return resp_data
                        desc = self._redact(str(resp_data.get("description", "Unknown error")))
                        raise TransportError(f"Telegram API sendMessage error: {desc}")
                    desc = self._redact(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    raise TransportError(f"Telegram HTTP failure: {desc}")
            except httpx.RequestError as exc:
                clean_exc = self._redact(str(exc))
                last_error = clean_exc
                logger.warning("Telegram sendMessage attempt %d failed: %s", attempt, clean_exc)
                if attempt < self._retry_attempts:
                    time.sleep(self._backoff * attempt)
            except Exception as exc:
                if isinstance(exc, TransportError):
                    raise
                clean_exc = self._redact(str(exc))
                raise TransportError(f"Unexpected transport error: {clean_exc}") from exc

        raise TransportError(f"sendMessage exhausted {self._retry_attempts} retries: {last_error}")
