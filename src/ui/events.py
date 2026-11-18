"""SAGE UI — Execution Event Bus.

Publishes typed execution events from SAGE internals to WebSocket clients and Desktop GUI.

Design:
- EventBus is a thread-safe in-process broker.
- Events are queued per-subscriber (each WebSocket gets its own asyncio.Queue).
- Desktop GUI subscribers can register callbacks via add_listener/remove_listener.
- All event emission is non-blocking.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    # Task lifecycle
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"

    # Step lifecycle
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"

    # Tool invocation
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_DENIED = "tool.denied"

    # TrustCore approval
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"

    # System / runtime
    RUNTIME_STARTED = "runtime.started"
    RUNTIME_STOPPED = "runtime.stopped"
    CHAT_MESSAGE = "chat.message"
    CHAT_RESPONSE = "chat.response"


# ---------------------------------------------------------------------------
# Event payload
# ---------------------------------------------------------------------------


@dataclass
class ExecutionEvent:
    event_type: str
    message: str = ""
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    component: str = "sage"
    status: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    event_id: str = field(default_factory=lambda: str(uuid4()))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# In-process EventBus
# ---------------------------------------------------------------------------

_MAX_QUEUE_SIZE = 200


class EventBus:
    """Thread-safe fan-out event broker for WebSocket subscribers and GUI listeners."""

    @classmethod
    def get_instance(cls) -> EventBus:
        return get_event_bus()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: Set[asyncio.Queue] = set()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register a synchronous listener callback for GUI components."""
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Unregister a listener callback."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def publish(self, event: ExecutionEvent) -> None:
        """Emit an event to all subscribers and listeners."""
        payload = event.to_dict()
        with self._lock:
            queues = list(self._queues)
            listeners = list(self._listeners)

        for q in queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

        for listener in listeners:
            try:
                listener(payload)
            except Exception as exc:
                logger.debug("Listener callback error: %s", exc)

    def emit(
        self,
        event_type: str,
        message: str = "",
        task_id: Optional[str] = None,
        step_id: Optional[str] = None,
        component: str = "sage",
        status: str = "",
        **metadata: Any,
    ) -> None:
        """Convenience helper: construct and publish an event."""
        self.publish(
            ExecutionEvent(
                event_type=event_type,
                message=message,
                task_id=task_id,
                step_id=step_id,
                component=component,
                status=status,
                metadata=metadata,
            )
        )


# Module-level singleton shared by server, desktop GUI and runtime bridge
_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
