"""SAGE Remote Control Gateway -- Self-Echo Suppression Registry.

Canonical Reference:
    D5: Self-Echo Suppression
"""

from __future__ import annotations

import threading
import time
from typing import Dict


class SelfEchoRegistry:
    """Thread-safe, bounded in-memory registry of outbound message IDs to suppress self-echoes.

    Capacity: 500 entries max.
    TTL: 3600 seconds (1 hour).
    """

    def __init__(self, max_size: int = 500, ttl_seconds: float = 3600.0) -> None:
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: Dict[str, float] = {}

    def _purge_expired_locked(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        expired = [msg_id for msg_id, ts in self._entries.items() if ts < cutoff]
        for msg_id in expired:
            del self._entries[msg_id]

    def register(self, message_id: str) -> None:
        """Register an outbound message ID to be suppressed if echoed back."""
        if not message_id or not isinstance(message_id, str):
            return
        msg_id = message_id.strip()
        if not msg_id:
            return

        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)

            if len(self._entries) >= self._max_size:
                # Evict oldest entry (FIFO)
                oldest_id = min(self._entries, key=lambda k: self._entries[k])
                del self._entries[oldest_id]

            self._entries[msg_id] = now

    def is_echo(self, message_id: str) -> bool:
        """Check if an inbound message ID was sent by us within the TTL."""
        if not message_id or not isinstance(message_id, str):
            return False
        msg_id = message_id.strip()
        if not msg_id:
            return False

        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            return msg_id in self._entries

    def __len__(self) -> int:
        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            return len(self._entries)
