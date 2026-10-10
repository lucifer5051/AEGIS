"""SAGE Supervisor & State Store -- SQLite-Backed Durable Store.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 27
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md

Guarantees:
- Transactional atomic writes using BEGIN IMMEDIATE.
- Strict monotonic state-transition enforcement for tasks and steps.
- Rejection of invalid or post-terminal state transitions.
- Zero-secret invariant: raises SecretLeakError on detected secrets.
- Output sanitization for persisted tool and execution results.
- Full offline, deterministic execution.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.manager.errors import (
    SessionNotFoundError,
    StateStoreError,
    StepNotFoundError,
    TaskNotFoundError,
    TaskStateError,
)
from src.manager.models import (
    CheckpointSnapshot,
    SessionRecord,
    SessionStatus,
    StepRecord,
    StepState,
    TaskRecord,
    TaskState,
)
from src.storage.database import transaction
from src.tools.sanitizer import sanitize_output
from src.trust.audit import _check_no_secrets
from src.trust.errors import SecretLeakError

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _sanitize_and_validate_json(data: Dict[str, Any]) -> str:
    """Serialize dictionary to JSON, verify no raw secrets, and return string.

    Raises:
        SecretLeakError: If secret pattern matches in serialized payload.
    """
    serialized = json.dumps(data, sort_keys=True)
    _check_no_secrets(serialized)
    return serialized


def _sanitize_and_validate_text(text: Optional[str]) -> Optional[str]:
    """Sanitize text and verify no secrets are present.

    Raises:
        SecretLeakError: If secret pattern matches.
    """
    if text is None:
        return None
    sanitized = sanitize_output(text)
    _check_no_secrets(sanitized)
    return sanitized


class StateStore:
    """SQLite-backed transactional persistence layer for tasks, steps, and checkpoints."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize state store with an active SQLite connection.

        Args:
            conn: An active SQLite database connection with WAL & foreign keys configured.
        """
        self._conn = conn

    # ==========================================================================
    # SUPERVISOR SESSIONS
    # ==========================================================================

    def create_session(
        self,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionRecord:
        """Create a new supervisor execution session.

        Raises:
            SecretLeakError: If secrets are detected in metadata.
            StateStoreError: If database persistence fails.
        """
        sid = session_id or str(uuid4())
        now = _now_utc()
        meta = metadata or {}
        meta_json = _sanitize_and_validate_json(meta)

        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    INSERT INTO supervisor_sessions (
                        session_id, status, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?);
                    """,
                    (sid, SessionStatus.ACTIVE.value, now, now, meta_json),
                )
        except SecretLeakError:
            raise
        except Exception as exc:
            raise StateStoreError(f"Failed to create supervisor session: {exc}") from exc

        return SessionRecord(
            session_id=sid,
            status=SessionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            metadata=meta,
        )

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        """Retrieve a supervisor session by ID."""
        cursor = self._conn.execute(
            """
            SELECT session_id, status, created_at, updated_at, metadata_json
            FROM supervisor_sessions
            WHERE session_id = ?;
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return SessionRecord(
            session_id=row[0],
            status=SessionStatus(row[1]),
            created_at=row[2],
            updated_at=row[3],
            metadata=json.loads(row[4]),
        )

    def update_session_status(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> SessionRecord:
        """Update the status of a supervisor session."""
        now = _now_utc()
        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    UPDATE supervisor_sessions
                    SET status = ?, updated_at = ?
                    WHERE session_id = ?;
                    """,
                    (status.value, now, session_id),
                )
                if cur.rowcount == 0:
                    raise SessionNotFoundError(f"Supervisor session not found: {session_id}")
        except SessionNotFoundError:
            raise
        except Exception as exc:
            raise StateStoreError(f"Failed to update session status: {exc}") from exc

        session = self.get_session(session_id)
        if not session:
            raise SessionNotFoundError(f"Supervisor session not found after update: {session_id}")
        return session

    # ==========================================================================
    # TASKS
    # ==========================================================================

    def create_task(
        self,
        title: str,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        priority: int = 100,
        routing_intent: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        execution_mode: Optional[Any] = None,
    ) -> TaskRecord:
        """Create a new task in SUBMITTED state.

        Raises:
            SecretLeakError: If secret pattern detected in title or metadata.
            StateStoreError: On database error.
        """
        tid = task_id or str(uuid4())
        clean_title = _sanitize_and_validate_text(title) or ""
        meta = dict(metadata or {})
        if execution_mode is not None:
            mode_str = (
                execution_mode.value if hasattr(execution_mode, "value") else str(execution_mode)
            )
            meta["execution_mode"] = mode_str
        meta_json = _sanitize_and_validate_json(meta)
        now = _now_utc()

        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    INSERT INTO tasks (
                        task_id, session_id, project_id, title, state, priority,
                        routing_intent, created_at, updated_at, completed_at,
                        error_summary, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?);
                    """,
                    (
                        tid,
                        session_id,
                        project_id,
                        clean_title,
                        TaskState.SUBMITTED.value,
                        priority,
                        routing_intent,
                        now,
                        now,
                        meta_json,
                    ),
                )
        except SecretLeakError:
            raise
        except Exception as exc:
            raise StateStoreError(f"Failed to create task '{tid}': {exc}") from exc

        return TaskRecord(
            task_id=tid,
            session_id=session_id,
            project_id=project_id,
            title=clean_title,
            state=TaskState.SUBMITTED,
            priority=priority,
            routing_intent=routing_intent,
            created_at=now,
            updated_at=now,
            metadata=meta,
        )

    def update_task_metadata(
        self,
        task_id: str,
        metadata: Dict[str, Any],
    ) -> TaskRecord:
        """Atomically update metadata JSON for a task.

        Raises:
            TaskNotFoundError: If task_id does not exist.
            SecretLeakError: If secret pattern detected in metadata.
            StateStoreError: On database error.
        """
        task = self.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        meta_json = _sanitize_and_validate_json(metadata)
        now = _now_utc()
        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET metadata_json = ?, updated_at = ?
                    WHERE task_id = ?;
                    """,
                    (meta_json, now, task_id),
                )
        except SecretLeakError:
            raise
        except Exception as exc:
            raise StateStoreError(f"Failed to update metadata for task '{task_id}': {exc}") from exc

        updated = self.get_task(task_id)
        if not updated:
            raise TaskNotFoundError(f"Task '{task_id}' missing after metadata update.")
        return updated

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Fetch a task record by ID."""
        cursor = self._conn.execute(
            """
            SELECT task_id, session_id, project_id, title, state, priority,
                   routing_intent, created_at, updated_at, completed_at,
                   error_summary, metadata_json
            FROM tasks
            WHERE task_id = ?;
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return TaskRecord(
            task_id=row[0],
            session_id=row[1],
            project_id=row[2],
            title=row[3],
            state=TaskState(row[4]),
            priority=row[5],
            routing_intent=row[6],
            created_at=row[7],
            updated_at=row[8],
            completed_at=row[9],
            error_summary=row[10],
            metadata=json.loads(row[11]),
        )

    def list_tasks(
        self,
        session_id: Optional[str] = None,
        state: Optional[TaskState] = None,
    ) -> List[TaskRecord]:
        """List tasks with optional filtering."""
        query = (
            "SELECT task_id, session_id, project_id, title, state, priority, "
            "routing_intent, created_at, updated_at, completed_at, "
            "error_summary, metadata_json FROM tasks WHERE 1=1"
        )
        params: List[Any] = []
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if state:
            query += " AND state = ?"
            params.append(state.value)
        query += " ORDER BY created_at ASC;"

        cursor = self._conn.execute(query, params)
        rows = cursor.fetchall()
        return [
            TaskRecord(
                task_id=r[0],
                session_id=r[1],
                project_id=r[2],
                title=r[3],
                state=TaskState(r[4]),
                priority=r[5],
                routing_intent=r[6],
                created_at=r[7],
                updated_at=r[8],
                completed_at=r[9],
                error_summary=r[10],
                metadata=json.loads(r[11]),
            )
            for r in rows
        ]

    def get_in_flight_tasks(self) -> List[TaskRecord]:
        """Fetch all tasks that are currently non-terminal (in-flight)."""
        cursor = self._conn.execute(
            """
            SELECT task_id, session_id, project_id, title, state, priority,
                   routing_intent, created_at, updated_at, completed_at,
                   error_summary, metadata_json
            FROM tasks
            WHERE state IN ('SUBMITTED', 'PLANNING', 'EXECUTING', 'AWAITING_APPROVAL')
            ORDER BY created_at ASC;
            """
        )
        rows = cursor.fetchall()
        return [
            TaskRecord(
                task_id=r[0],
                session_id=r[1],
                project_id=r[2],
                title=r[3],
                state=TaskState(r[4]),
                priority=r[5],
                routing_intent=r[6],
                created_at=r[7],
                updated_at=r[8],
                completed_at=r[9],
                error_summary=r[10],
                metadata=json.loads(r[11]),
            )
            for r in rows
        ]

    def transition_task_state(
        self,
        task_id: str,
        target_state: TaskState,
        error_summary: Optional[str] = None,
    ) -> TaskRecord:
        """Validate and execute a task state transition.

        Preconditions & Invariants:
        - Task must exist.
        - Terminal states cannot transition to any state.
        - Transition must be defined in VALID_TASK_TRANSITIONS.
        - Target state terminal automatically sets completed_at.

        Raises:
            TaskNotFoundError: If task_id does not exist.
            TaskStateError: If transition is invalid or mutates terminal state.
            SecretLeakError: If error_summary contains secrets.
        """
        task = self.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        if task.state == target_state:
            # Idempotent no-op
            return task

        if task.state.is_terminal:
            raise TaskStateError(
                f"Cannot transition task '{task_id}' from terminal state '{task.state.value}' "
                f"to '{target_state.value}'."
            )

        if not task.state.can_transition_to(target_state):
            raise TaskStateError(
                f"Illegal task transition for '{task_id}': "
                f"'{task.state.value}' -> '{target_state.value}'."
            )

        now = _now_utc()
        completed_at = now if target_state.is_terminal else None
        clean_error = _sanitize_and_validate_text(error_summary)

        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET state = ?, updated_at = ?, completed_at = ?, error_summary = ?
                    WHERE task_id = ?;
                    """,
                    (target_state.value, now, completed_at, clean_error, task_id),
                )
        except SecretLeakError:
            raise
        except Exception as exc:
            raise StateStoreError(f"Failed to transition task '{task_id}': {exc}") from exc

        updated = self.get_task(task_id)
        if not updated:
            raise TaskNotFoundError(f"Task '{task_id}' missing after state update.")
        return updated

    # ==========================================================================
    # TASK STEPS
    # ==========================================================================

    def create_step(
        self,
        task_id: str,
        step_index: int,
        action_name: str,
        step_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        is_idempotent: bool = False,
        idempotency_key: Optional[str] = None,
        capability_id: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> StepRecord:
        """Create a new step for a task in PENDING state.

        Raises:
            SecretLeakError: If secret pattern detected in arguments or action_name.
            StateStoreError: On database error or duplicate step index.
        """
        sid = step_id or str(uuid4())
        clean_action = _sanitize_and_validate_text(action_name) or ""
        clean_key = _sanitize_and_validate_text(idempotency_key)
        args = arguments or {}
        args_json = _sanitize_and_validate_json(args)
        now = _now_utc()

        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    INSERT INTO task_steps (
                        step_id, task_id, step_index, action_name, tool_name,
                        state, is_idempotent, idempotency_key, capability_id,
                        arguments_json, result_summary, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL);
                    """,
                    (
                        sid,
                        task_id,
                        step_index,
                        clean_action,
                        tool_name,
                        StepState.PENDING.value,
                        1 if is_idempotent else 0,
                        clean_key,
                        capability_id,
                        args_json,
                        now,
                        now,
                    ),
                )
        except SecretLeakError:
            raise
        except sqlite3.IntegrityError as exc:
            raise StateStoreError(f"Step index constraint or foreign key violation: {exc}") from exc
        except Exception as exc:
            raise StateStoreError(f"Failed to create step '{sid}': {exc}") from exc

        return StepRecord(
            step_id=sid,
            task_id=task_id,
            step_index=step_index,
            action_name=clean_action,
            tool_name=tool_name,
            state=StepState.PENDING,
            is_idempotent=is_idempotent,
            idempotency_key=clean_key,
            capability_id=capability_id,
            arguments=args,
            created_at=now,
            updated_at=now,
        )

    def get_step(self, step_id: str) -> Optional[StepRecord]:
        """Fetch a step record by step_id."""
        cursor = self._conn.execute(
            """
            SELECT step_id, task_id, step_index, action_name, tool_name,
                   state, is_idempotent, idempotency_key, capability_id,
                   arguments_json, result_summary, created_at, updated_at, completed_at
            FROM task_steps
            WHERE step_id = ?;
            """,
            (step_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return StepRecord(
            step_id=row[0],
            task_id=row[1],
            step_index=row[2],
            action_name=row[3],
            tool_name=row[4],
            state=StepState(row[5]),
            is_idempotent=bool(row[6]),
            idempotency_key=row[7],
            capability_id=row[8],
            arguments=json.loads(row[9]),
            result_summary=row[10],
            created_at=row[11],
            updated_at=row[12],
            completed_at=row[13],
        )

    def list_task_steps(self, task_id: str) -> List[StepRecord]:
        """List all steps for a task ordered by step_index ASC."""
        cursor = self._conn.execute(
            """
            SELECT step_id, task_id, step_index, action_name, tool_name,
                   state, is_idempotent, idempotency_key, capability_id,
                   arguments_json, result_summary, created_at, updated_at, completed_at
            FROM task_steps
            WHERE task_id = ?
            ORDER BY step_index ASC;
            """,
            (task_id,),
        )
        rows = cursor.fetchall()
        return [
            StepRecord(
                step_id=r[0],
                task_id=r[1],
                step_index=r[2],
                action_name=r[3],
                tool_name=r[4],
                state=StepState(r[5]),
                is_idempotent=bool(r[6]),
                idempotency_key=r[7],
                capability_id=r[8],
                arguments=json.loads(r[9]),
                result_summary=r[10],
                created_at=r[11],
                updated_at=r[12],
                completed_at=r[13],
            )
            for r in rows
        ]

    def transition_step_state(
        self,
        step_id: str,
        target_state: StepState,
        result_summary: Optional[str] = None,
        capability_id: Optional[str] = None,
    ) -> StepRecord:
        """Validate and execute a step state transition.

        Preconditions & Invariants:
        - Step must exist.
        - Terminal steps cannot transition to another state.
        - Valid transition must be defined in VALID_STEP_TRANSITIONS.
        - Target state terminal automatically sets completed_at.

        Raises:
            StepNotFoundError: If step_id does not exist.
            TaskStateError: If transition is invalid or mutates terminal step.
            SecretLeakError: If result_summary contains secrets.
        """
        step = self.get_step(step_id)
        if not step:
            raise StepNotFoundError(f"Step not found: {step_id}")

        if step.state == target_state:
            # Idempotent no-op
            return step

        if step.state.is_terminal:
            raise TaskStateError(
                f"Cannot transition step '{step_id}' from terminal state '{step.state.value}' "
                f"to '{target_state.value}'."
            )

        if not step.state.can_transition_to(target_state):
            raise TaskStateError(
                f"Illegal step transition for '{step_id}': "
                f"'{step.state.value}' -> '{target_state.value}'."
            )

        now = _now_utc()
        completed_at = now if target_state.is_terminal else None
        clean_result = (
            _sanitize_and_validate_text(result_summary) if result_summary else step.result_summary
        )
        cap_id = capability_id or step.capability_id

        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    UPDATE task_steps
                    SET state = ?, result_summary = ?, capability_id = ?,
                        updated_at = ?, completed_at = ?
                    WHERE step_id = ?;
                    """,
                    (target_state.value, clean_result, cap_id, now, completed_at, step_id),
                )
        except SecretLeakError:
            raise
        except Exception as exc:
            raise StateStoreError(f"Failed to transition step '{step_id}': {exc}") from exc

        updated = self.get_step(step_id)
        if not updated:
            raise StepNotFoundError(f"Step '{step_id}' missing after state update.")
        return updated

    def update_step_arguments(
        self,
        step_id: str,
        arguments: Dict[str, Any],
    ) -> StepRecord:
        """Atomically update arguments JSON for a step.

        Raises:
            StepNotFoundError: If step_id does not exist.
            SecretLeakError: If secret pattern detected in arguments.
            StateStoreError: On database error.
        """
        step = self.get_step(step_id)
        if not step:
            raise StepNotFoundError(f"Step not found: {step_id}")

        args_json = _sanitize_and_validate_json(arguments)
        now = _now_utc()
        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    UPDATE task_steps
                    SET arguments_json = ?, updated_at = ?
                    WHERE step_id = ?;
                    """,
                    (args_json, now, step_id),
                )
        except SecretLeakError:
            raise
        except Exception as exc:
            raise StateStoreError(
                f"Failed to update arguments for step '{step_id}': {exc}"
            ) from exc

        updated = self.get_step(step_id)
        if not updated:
            raise StepNotFoundError(f"Step '{step_id}' missing after arguments update.")
        return updated

    # ==========================================================================
    # CHECKPOINTS
    # ==========================================================================

    def create_checkpoint(
        self,
        task_id: str,
        checkpoint_seq: int,
        snapshot: Dict[str, Any],
        step_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
    ) -> CheckpointSnapshot:
        """Persist an idempotent checkpoint snapshot.

        Raises:
            SecretLeakError: If secrets are detected in snapshot.
            StateStoreError: On database error or sequence conflict.
        """
        cid = checkpoint_id or str(uuid4())
        now = _now_utc()
        snap_json = _sanitize_and_validate_json(snapshot)

        try:
            with transaction(self._conn, immediate=True) as cur:
                cur.execute(
                    """
                    INSERT INTO task_checkpoints (
                        checkpoint_id, task_id, step_id, checkpoint_seq, snapshot_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (cid, task_id, step_id, checkpoint_seq, snap_json, now),
                )
        except SecretLeakError:
            raise
        except sqlite3.IntegrityError as exc:
            raise StateStoreError(
                f"Duplicate checkpoint sequence {checkpoint_seq} for task '{task_id}': {exc}"
            ) from exc
        except Exception as exc:
            raise StateStoreError(f"Failed to create checkpoint '{cid}': {exc}") from exc

        return CheckpointSnapshot(
            checkpoint_id=cid,
            task_id=task_id,
            step_id=step_id,
            checkpoint_seq=checkpoint_seq,
            snapshot=snapshot,
            created_at=now,
        )

    def get_latest_checkpoint(self, task_id: str) -> Optional[CheckpointSnapshot]:
        """Fetch the most recent checkpoint for a task."""
        cursor = self._conn.execute(
            """
            SELECT checkpoint_id, task_id, step_id, checkpoint_seq, snapshot_json, created_at
            FROM task_checkpoints
            WHERE task_id = ?
            ORDER BY checkpoint_seq DESC
            LIMIT 1;
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return CheckpointSnapshot(
            checkpoint_id=row[0],
            task_id=row[1],
            step_id=row[2],
            checkpoint_seq=row[3],
            snapshot=json.loads(row[4]),
            created_at=row[5],
        )

    def list_checkpoints(self, task_id: str) -> List[CheckpointSnapshot]:
        """List all checkpoints for a task ordered by checkpoint_seq ASC."""
        cursor = self._conn.execute(
            """
            SELECT checkpoint_id, task_id, step_id, checkpoint_seq, snapshot_json, created_at
            FROM task_checkpoints
            WHERE task_id = ?
            ORDER BY checkpoint_seq ASC;
            """,
            (task_id,),
        )
        rows = cursor.fetchall()
        return [
            CheckpointSnapshot(
                checkpoint_id=r[0],
                task_id=r[1],
                step_id=r[2],
                checkpoint_seq=r[3],
                snapshot=json.loads(r[4]),
                created_at=r[5],
            )
            for r in rows
        ]
