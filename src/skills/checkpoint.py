"""SAGE Skills Subsystem -- Checkpoint Integration.

Integrates SkillInvocation execution with the SAGE Supervisor StateStore checkpoint system.
Follows the research reference "AgentRewind" for deterministic boundary persistence:
- Captures skill_id, invocation_id, step_index, execution_state, and recovery_policy.
- Lightweight and deterministic; avoids creating excessive checkpoints for short tasks.
- Integrates cleanly with StateStore.create_checkpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field

from src.manager.models import CheckpointSnapshot
from src.manager.state_store import StateStore


class SkillCheckpointMetadata(BaseModel):
    """Structured checkpoint payload identifying skill execution boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(description="Executing skill ID.")
    invocation_id: str = Field(description="Invocation identifier.")
    step_index: int = Field(ge=0, description="Step index within execution plan.")
    execution_state: Dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque serializable step state dictionary.",
    )
    recovery_policy: str = Field(
        description="Declared recovery policy (e.g. 'RETRY_IDEMPOTENT', 'ROLLBACK_CHECKPOINT')."
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp of checkpoint.",
    )

    def to_snapshot_dict(self) -> Dict[str, Any]:
        """Convert metadata to a serializable dictionary for StateStore snapshot."""
        return {
            "type": "SKILL_CHECKPOINT",
            "skill_id": self.skill_id,
            "invocation_id": self.invocation_id,
            "step_index": self.step_index,
            "execution_state": self.execution_state,
            "recovery_policy": self.recovery_policy,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_snapshot_dict(cls, data: Dict[str, Any]) -> "SkillCheckpointMetadata":
        """Reconstruct SkillCheckpointMetadata from a snapshot dictionary."""
        return cls(
            skill_id=data["skill_id"],
            invocation_id=data["invocation_id"],
            step_index=data["step_index"],
            execution_state=data.get("execution_state", {}),
            recovery_policy=data.get("recovery_policy", "FAIL_FAST"),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        )


def persist_skill_checkpoint(
    state_store: StateStore,
    task_id: str,
    step_id: str,
    checkpoint_seq: int,
    metadata: SkillCheckpointMetadata,
) -> CheckpointSnapshot:
    """Store a skill execution boundary snapshot using the Supervisor StateStore.

    Returns:
        CheckpointSnapshot persisted in SQLite.
    """
    snapshot = metadata.to_snapshot_dict()
    return state_store.create_checkpoint(
        task_id=task_id,
        step_id=step_id,
        checkpoint_seq=checkpoint_seq,
        snapshot=snapshot,
    )
