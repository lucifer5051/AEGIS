"""SAGE Desktop Awareness Subsystem -- Element Registry & Locality Tuples.

Implements resilient element tracking and stale reference detection across turns.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalityTuple:
    """Persistent, multi-attribute element descriptor ensuring resilience against dynamic rendering."""

    process_name: str
    root_hwnd: int
    control_type: str
    automation_id: str
    name: str
    runtime_id: Tuple[int, ...] = field(default_factory=tuple)
    session_generation: int = 1
    created_at: float = field(default_factory=time.time)


class DesktopElementRegistry:
    """Tracks active UI Automation elements, detects stale handles, and determines reacquisition needs."""

    def __init__(self, stale_ttl_seconds: float = 60.0) -> None:
        self._stale_ttl = stale_ttl_seconds
        self._registered_elements: Dict[str, LocalityTuple] = {}

    def register_element(
        self,
        key: str,
        process_name: str,
        root_hwnd: int,
        control_type: str,
        automation_id: str,
        name: str,
        runtime_id: Optional[List[int]] = None,
        session_generation: int = 1,
    ) -> LocalityTuple:
        """Register a recognized element with its locality tuple."""
        tup = LocalityTuple(
            process_name=process_name.lower(),
            root_hwnd=root_hwnd,
            control_type=control_type,
            automation_id=automation_id,
            name=name,
            runtime_id=tuple(runtime_id or []),
            session_generation=session_generation,
            created_at=time.time(),
        )
        self._registered_elements[key] = tup
        return tup

    def is_reference_stale(
        self,
        key: str,
        current_session_generation: int,
        live_open_hwnds: Optional[List[int]] = None,
    ) -> Tuple[bool, str]:
        """Verify whether an element reference is stale and requires reacquisition.

        Returns (is_stale, reason).
        """
        descriptor = self._registered_elements.get(key)
        if not descriptor:
            return True, f"Element '{key}' was never registered."

        # Session mismatch check
        if descriptor.session_generation != current_session_generation:
            return True, (
                f"Session generation mismatch (element={descriptor.session_generation}, "
                f"current={current_session_generation}); observer process restarted."
            )

        # Window destruction check
        if live_open_hwnds is not None and descriptor.root_hwnd > 0:
            if descriptor.root_hwnd not in live_open_hwnds:
                return True, f"Parent window HWND={descriptor.root_hwnd} is no longer open."

        # Time-to-live expiration check
        age = time.time() - descriptor.created_at
        if age > self._stale_ttl:
            return True, f"Element reference expired after {age:.1f}s (> {self._stale_ttl:.1f}s TTL)."

        return False, "Reference is valid and fresh."

    def get_element(self, key: str) -> Optional[LocalityTuple]:
        return self._registered_elements.get(key)

    def invalidate(self, key: str) -> None:
        self._registered_elements.pop(key, None)
