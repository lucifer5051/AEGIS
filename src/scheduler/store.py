"""SAGE Durable Scheduler -- SQLite Persistence Store.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12

Persists schedule definitions and execution history to SQLite with deduplication guarantees.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.scheduler.errors import DuplicateScheduleError
from src.scheduler.models import (
    MissedRunPolicy,
    ScheduleDefinition,
    ScheduleExecutionRecord,
    ScheduleExecutionStatus,
    ScheduleStatus,
    ScheduleType,
)
from src.storage.database import transaction
from src.trust.audit import _check_no_secrets

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _sanitize_and_serialize_json(data: Dict[str, Any]) -> str:
    """Serialize dictionary to JSON while verifying zero raw secrets."""
    serialized = json.dumps(data, sort_keys=True)
    _check_no_secrets(serialized)
    return serialized


class SchedulerStore:
    """SQLite-backed transactional persistence layer for schedules and execution audit."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize scheduler store with active SQLite connection."""
        self._conn = conn

    # ==========================================================================
    # SCHEDULE DEFINITIONS
    # ==========================================================================

    def save_schedule(self, schedule: ScheduleDefinition) -> ScheduleDefinition:
        """Insert or update a schedule definition."""
        params_json = _sanitize_and_serialize_json(schedule.parameters)
        condition_json = (
            _sanitize_and_serialize_json(schedule.trigger_condition)
            if schedule.trigger_condition is not None
            else None
        )

        sql = (
            "INSERT INTO schedules "
            "(schedule_id, project_id, workflow_id, target_action, schedule_type, "
            " cron_expression, interval_seconds, trigger_condition_json, next_run_at, "
            " last_run_at, missed_run_policy, status, is_enabled, parameters_json, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET "
            "  project_id = excluded.project_id, "
            "  workflow_id = excluded.workflow_id, "
            "  target_action = excluded.target_action, "
            "  schedule_type = excluded.schedule_type, "
            "  cron_expression = excluded.cron_expression, "
            "  interval_seconds = excluded.interval_seconds, "
            "  trigger_condition_json = excluded.trigger_condition_json, "
            "  next_run_at = excluded.next_run_at, "
            "  last_run_at = excluded.last_run_at, "
            "  missed_run_policy = excluded.missed_run_policy, "
            "  status = excluded.status, "
            "  is_enabled = excluded.is_enabled, "
            "  parameters_json = excluded.parameters_json, "
            "  updated_at = excluded.updated_at;"
        )

        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(
                sql,
                (
                    schedule.schedule_id,
                    schedule.project_id,
                    schedule.workflow_id,
                    schedule.target_action,
                    schedule.schedule_type.value,
                    schedule.cron_expression,
                    schedule.interval_seconds,
                    condition_json,
                    schedule.next_run_at,
                    schedule.last_run_at,
                    schedule.missed_run_policy.value,
                    schedule.status.value,
                    1 if schedule.is_enabled else 0,
                    params_json,
                    schedule.created_at,
                    schedule.updated_at,
                ),
            )

        return schedule

    def get_schedule(self, schedule_id: str) -> Optional[ScheduleDefinition]:
        """Retrieve a schedule definition by ID."""
        sql = (
            "SELECT schedule_id, project_id, workflow_id, target_action, "
            "       schedule_type, cron_expression, interval_seconds, "
            "       trigger_condition_json, next_run_at, last_run_at, "
            "       missed_run_policy, status, is_enabled, parameters_json, "
            "       created_at, updated_at "
            "FROM schedules WHERE schedule_id = ?;"
        )
        row = self._conn.execute(sql, (schedule_id,)).fetchone()
        if row is None:
            return None

        trigger_cond = (
            json.loads(row["trigger_condition_json"]) if row["trigger_condition_json"] else None
        )
        params = json.loads(row["parameters_json"])

        return ScheduleDefinition(
            schedule_id=row["schedule_id"],
            project_id=row["project_id"],
            workflow_id=row["workflow_id"],
            target_action=row["target_action"],
            schedule_type=ScheduleType(row["schedule_type"]),
            cron_expression=row["cron_expression"],
            interval_seconds=row["interval_seconds"],
            trigger_condition=trigger_cond,
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            missed_run_policy=MissedRunPolicy(row["missed_run_policy"]),
            status=ScheduleStatus(row["status"]),
            is_enabled=bool(row["is_enabled"]),
            parameters=params,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_schedules(
        self,
        project_id: Optional[str] = None,
        status: Optional[ScheduleStatus] = None,
        due_before: Optional[str] = None,
    ) -> List[ScheduleDefinition]:
        """List schedule definitions with optional filters."""
        conditions = []
        params: List[Any] = []

        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)
        if due_before is not None:
            conditions.append("next_run_at <= ?")
            params.append(due_before)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT schedule_id, project_id, workflow_id, target_action, "
            f"       schedule_type, cron_expression, interval_seconds, "
            f"       trigger_condition_json, next_run_at, last_run_at, "
            f"       missed_run_policy, status, is_enabled, parameters_json, "
            f"       created_at, updated_at "
            f"FROM schedules {where_clause} "
            f"ORDER BY next_run_at ASC;"
        )

        rows = self._conn.execute(sql, params).fetchall()
        schedules: List[ScheduleDefinition] = []
        for row in rows:
            trigger_cond = (
                json.loads(row["trigger_condition_json"]) if row["trigger_condition_json"] else None
            )
            parameters = json.loads(row["parameters_json"])
            schedules.append(
                ScheduleDefinition(
                    schedule_id=row["schedule_id"],
                    project_id=row["project_id"],
                    workflow_id=row["workflow_id"],
                    target_action=row["target_action"],
                    schedule_type=ScheduleType(row["schedule_type"]),
                    cron_expression=row["cron_expression"],
                    interval_seconds=row["interval_seconds"],
                    trigger_condition=trigger_cond,
                    next_run_at=row["next_run_at"],
                    last_run_at=row["last_run_at"],
                    missed_run_policy=MissedRunPolicy(row["missed_run_policy"]),
                    status=ScheduleStatus(row["status"]),
                    is_enabled=bool(row["is_enabled"]),
                    parameters=parameters,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        return schedules

    def update_schedule_status(
        self,
        schedule_id: str,
        status: ScheduleStatus,
    ) -> None:
        """Update the operational status of a schedule."""
        now = _now_utc()
        sql = "UPDATE schedules SET status = ?, updated_at = ? WHERE schedule_id = ?;"
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, (status.value, now, schedule_id))

    def update_schedule_next_run(
        self,
        schedule_id: str,
        next_run_at: str,
        last_run_at: Optional[str] = None,
        status: Optional[ScheduleStatus] = None,
    ) -> None:
        """Update the next run timestamp and optional last run / status."""
        now = _now_utc()
        args: tuple[Any, ...]
        if status is not None:
            sql = (
                "UPDATE schedules SET next_run_at = ?, last_run_at = ?, "
                "status = ?, updated_at = ? WHERE schedule_id = ?;"
            )
            args = (next_run_at, last_run_at, status.value, now, schedule_id)
        else:
            sql = (
                "UPDATE schedules SET next_run_at = ?, last_run_at = ?, "
                "updated_at = ? WHERE schedule_id = ?;"
            )
            args = (next_run_at, last_run_at, now, schedule_id)

        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(sql, args)

    # ==========================================================================
    # SCHEDULE EXECUTIONS & DEDUPLICATION
    # ==========================================================================

    def has_execution_for_slot(self, schedule_id: str, scheduled_for: str) -> bool:
        """Check whether an execution attempt is already recorded for this scheduled time slot.

        Prevents duplicate triggers on host restart or repeated polling.
        """
        sql = (
            "SELECT 1 FROM schedule_executions WHERE schedule_id = ? AND scheduled_for = ? LIMIT 1;"
        )
        row = self._conn.execute(sql, (schedule_id, scheduled_for)).fetchone()
        return row is not None

    def record_execution(
        self,
        execution: ScheduleExecutionRecord,
    ) -> ScheduleExecutionRecord:
        """Record an execution attempt, failing closed if slot is duplicate."""
        sql = (
            "INSERT INTO schedule_executions "
            "(execution_id, schedule_id, run_id, scheduled_for, triggered_at, "
            " status, result_summary, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);"
        )
        try:
            with transaction(self._conn, immediate=True) as cursor:
                cursor.execute(
                    sql,
                    (
                        execution.execution_id,
                        execution.schedule_id,
                        execution.run_id,
                        execution.scheduled_for,
                        execution.triggered_at,
                        execution.status.value,
                        execution.result_summary,
                        execution.created_at,
                        execution.completed_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateScheduleError(
                f"Execution slot for schedule '{execution.schedule_id}' at "
                f"'{execution.scheduled_for}' already exists: {exc}"
            ) from exc

        return execution

    def update_execution(
        self,
        execution: ScheduleExecutionRecord,
    ) -> ScheduleExecutionRecord:
        """Update status and results of an existing schedule execution."""
        sql = (
            "UPDATE schedule_executions SET "
            "  run_id = ?, "
            "  status = ?, "
            "  result_summary = ?, "
            "  completed_at = ? "
            "WHERE execution_id = ?;"
        )
        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(
                sql,
                (
                    execution.run_id,
                    execution.status.value,
                    execution.result_summary,
                    execution.completed_at,
                    execution.execution_id,
                ),
            )
        return execution

    def get_execution(self, execution_id: str) -> Optional[ScheduleExecutionRecord]:
        """Fetch an execution record by execution_id."""
        sql = (
            "SELECT execution_id, schedule_id, run_id, scheduled_for, triggered_at, "
            "       status, result_summary, created_at, completed_at "
            "FROM schedule_executions WHERE execution_id = ?;"
        )
        row = self._conn.execute(sql, (execution_id,)).fetchone()
        if row is None:
            return None

        return ScheduleExecutionRecord(
            execution_id=row["execution_id"],
            schedule_id=row["schedule_id"],
            run_id=row["run_id"],
            scheduled_for=row["scheduled_for"],
            triggered_at=row["triggered_at"],
            status=ScheduleExecutionStatus(row["status"]),
            result_summary=row["result_summary"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def list_executions(
        self,
        schedule_id: Optional[str] = None,
    ) -> List[ScheduleExecutionRecord]:
        """List execution audit records."""
        if schedule_id is not None:
            sql = (
                "SELECT execution_id, schedule_id, run_id, scheduled_for, triggered_at, "
                "       status, result_summary, created_at, completed_at "
                "FROM schedule_executions WHERE schedule_id = ? "
                "ORDER BY scheduled_for DESC;"
            )
            rows = self._conn.execute(sql, (schedule_id,)).fetchall()
        else:
            sql = (
                "SELECT execution_id, schedule_id, run_id, scheduled_for, triggered_at, "
                "       status, result_summary, created_at, completed_at "
                "FROM schedule_executions ORDER BY scheduled_for DESC;"
            )
            rows = self._conn.execute(sql).fetchall()

        return [
            ScheduleExecutionRecord(
                execution_id=r["execution_id"],
                schedule_id=r["schedule_id"],
                run_id=r["run_id"],
                scheduled_for=r["scheduled_for"],
                triggered_at=r["triggered_at"],
                status=ScheduleExecutionStatus(r["status"]),
                result_summary=r["result_summary"],
                created_at=r["created_at"],
                completed_at=r["completed_at"],
            )
            for r in rows
        ]
