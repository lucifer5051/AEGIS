"""SAGE Remote Control Gateway -- Inbound Rate Limiter.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 3 (Channels)
    PROMPT 032: SAGE Secure Remote-Control Gateway (Telegram-First)

Invariants:
- Deterministic sliding-window algorithm.
- Bounded memory: automatically purges expired timestamps.
- Thread-safe using threading.Lock.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, List


class SlidingWindowRateLimiter:
    """Sliding-window in-memory rate limiter per user ID."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._user_timestamps: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(
        self,
        user_id: int | str,
        limit: int = 20,
        window_seconds: float = 60.0,
    ) -> bool:
        """Check if a user is within their rate limit without consuming quota."""
        uid = str(user_id)
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._user_timestamps.get(uid, [])
            valid_timestamps = [t for t in timestamps if t > cutoff]
            return len(valid_timestamps) < limit

    def record_and_check(
        self,
        user_id: int | str,
        limit: int = 20,
        window_seconds: float = 60.0,
    ) -> bool:
        """Atomically record a request and return True if allowed, False if exceeded."""
        uid = str(user_id)
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._user_timestamps[uid]
            # Prune expired timestamps
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= limit:
                self._user_timestamps[uid] = timestamps
                return False
            timestamps.append(now)
            self._user_timestamps[uid] = timestamps
            return True

    def reset(self, user_id: int | str | None = None) -> None:
        """Clear rate limit history for a specific user or all users."""
        with self._lock:
            if user_id is not None:
                self._user_timestamps.pop(str(user_id), None)
            else:
                self._user_timestamps.clear()
