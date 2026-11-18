"""SAGE Desktop Awareness Subsystem -- Task Continuity Manager.

Manages task suspension, state checkpointing, and safe resumption during interruptions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class DesktopTaskCheckpoint:
    task_id: str
    step_index: int
    target_process: str
    target_hwnd: int
    last_known_title: str
    paused_at: float = field(default_factory=time.time)
    pause_reason: str = ""
    resume_attempts: int = 0
    state_payload: Dict[str, Any] = field(default_factory=dict)


class TaskContinuityManager:
    """Manages the continuity lifecycle of desktop automation tasks across interruptions."""

    def __init__(self, state_store: Optional[Any] = None) -> None:
        self._checkpoints: Dict[str, DesktopTaskCheckpoint] = {}
        self._state_store = state_store

    def checkpoint_task(
        self,
        task_id: str,
        step_index: int,
        target_process: str,
        target_hwnd: int,
        last_known_title: str,
        pause_reason: str,
        state_payload: Optional[Dict[str, Any]] = None,
    ) -> DesktopTaskCheckpoint:
        """Record a checkpoint when an automated task is paused due to an interruption."""
        cp = DesktopTaskCheckpoint(
            task_id=task_id,
            step_index=step_index,
            target_process=target_process,
            target_hwnd=target_hwnd,
            last_known_title=last_known_title,
            pause_reason=pause_reason,
            state_payload=state_payload or {},
        )
        self._checkpoints[task_id] = cp
        logger.info("Checkpointed paused desktop task '%s': %s", task_id, pause_reason)

        if self._state_store is not None:
            try:
                self._state_store.create_checkpoint(
                    task_id=task_id,
                    checkpoint_seq=step_index,
                    snapshot={
                        "task_id": task_id,
                        "step_index": step_index,
                        "target_process": target_process,
                        "target_hwnd": target_hwnd,
                        "last_known_title": last_known_title,
                        "pause_reason": pause_reason,
                        "state_payload": state_payload or {},
                    },
                )
            except Exception as exc:
                logger.warning("Failed to persist desktop checkpoint to StateStore: %s", exc)

        return cp

    def get_checkpoint(self, task_id: str) -> Optional[DesktopTaskCheckpoint]:
        return self._checkpoints.get(task_id)

    def can_resume(self, task_id: str, current_active_hwnd: int, current_active_proc: str) -> bool:
        """Check whether desktop environment is safe to resume the paused task."""
        cp = self._checkpoints.get(task_id)
        if not cp:
            return False

        # Can resume if active process matches or window has returned
        if current_active_proc.lower() == cp.target_process.lower():
            return True
        if current_active_hwnd == cp.target_hwnd and cp.target_hwnd > 0:
            return True

        return False

    def clear_checkpoint(self, task_id: str) -> None:
        self._checkpoints.pop(task_id, None)
