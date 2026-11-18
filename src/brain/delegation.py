"""DelegationEngine — Controlled agent-to-agent delegation and safety boundaries.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & Independent Multi-Agent System
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.brain.models import DelegationRecord, DelegationStatus

logger = logging.getLogger(__name__)

MAX_DELEGATION_DEPTH = 3


class DelegationSecurityError(Exception):
    """Raised when delegation violates security, cycles or depth invariants."""


class DelegationEngine:
    """Manages secure, auditable delegation of tasks between independent agents."""

    def __init__(self, max_depth: int = MAX_DELEGATION_DEPTH) -> None:
        self._lock = threading.RLock()
        self._max_depth = max_depth
        self._delegations: Dict[str, DelegationRecord] = {}
        # Tracks active delegation chains: correlation_id -> list of agent_ids visited
        self._active_chains: Dict[str, List[str]] = {}

    def create_delegation(
        self,
        source_agent_id: str,
        destination_agent_id: str,
        assignment: str,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        shared_context_refs: Optional[List[str]] = None,
        expected_output: str = "",
    ) -> DelegationRecord:
        """Create and validate a new delegation assignment."""
        with self._lock:
            # Self-delegation check
            if source_agent_id == destination_agent_id:
                raise DelegationSecurityError(
                    f"Agent '{source_agent_id}' cannot delegate directly to itself."
                )

            cid = correlation_id or str(uuid4())
            chain = self._active_chains.get(cid, [source_agent_id])

            # 1. Circular Delegation Prevention
            if destination_agent_id in chain:
                raise DelegationSecurityError(
                    f"Circular delegation detected in correlation chain {cid}: "
                    f"{' -> '.join(chain)} -> {destination_agent_id}"
                )

            # 2. Maximum Delegation Depth
            if len(chain) >= self._max_depth:
                raise DelegationSecurityError(
                    f"Delegation depth limit ({self._max_depth}) exceeded for chain: "
                    f"{' -> '.join(chain)}"
                )

            # Record in chain
            updated_chain = list(chain) + [destination_agent_id]
            self._active_chains[cid] = updated_chain

            record = DelegationRecord(
                delegation_id=str(uuid4()),
                correlation_id=cid,
                task_id=task_id,
                parent_task_id=parent_task_id,
                source_agent_id=source_agent_id,
                destination_agent_id=destination_agent_id,
                assignment=assignment,
                shared_context_refs=shared_context_refs or [],
                expected_output=expected_output,
                status=DelegationStatus.PENDING,
            )
            self._delegations[record.delegation_id] = record

            logger.info(
                "Delegation created: %s -> %s (ID: %s, Correlation: %s)",
                source_agent_id,
                destination_agent_id,
                record.delegation_id,
                cid,
            )
            return record

    def update_status(
        self,
        delegation_id: str,
        status: DelegationStatus,
        result: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> DelegationRecord:
        """Update status and result of a delegation record."""
        with self._lock:
            record = self._delegations.get(delegation_id)
            if not record:
                raise ValueError(f"Delegation record '{delegation_id}' not found.")

            completed_at = (
                datetime.now(timezone.utc).isoformat()
                if status in (DelegationStatus.COMPLETED, DelegationStatus.FAILED, DelegationStatus.REJECTED)
                else None
            )

            updated = record.model_copy(
                update={
                    "status": status,
                    "result": result,
                    "error_message": error_message,
                    "completed_at": completed_at,
                }
            )
            self._delegations[delegation_id] = updated

            # If terminated, release destination from active chain
            if completed_at and record.correlation_id in self._active_chains:
                chain = self._active_chains[record.correlation_id]
                if record.destination_agent_id in chain:
                    chain.remove(record.destination_agent_id)
                if not chain:
                    del self._active_chains[record.correlation_id]

            return updated

    def get_delegation(self, delegation_id: str) -> Optional[DelegationRecord]:
        """Fetch delegation by ID."""
        with self._lock:
            return self._delegations.get(delegation_id)

    def list_delegations(
        self,
        source_agent_id: Optional[str] = None,
        destination_agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> List[DelegationRecord]:
        """Query delegation records with optional filters."""
        with self._lock:
            results = list(self._delegations.values())
            if source_agent_id:
                results = [r for r in results if r.source_agent_id == source_agent_id]
            if destination_agent_id:
                results = [r for r in results if r.destination_agent_id == destination_agent_id]
            if task_id:
                results = [r for r in results if r.task_id == task_id or r.parent_task_id == task_id]
            return results
