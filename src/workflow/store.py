"""SAGE Durable Workflow Engine -- SQLite Persistence Store.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 11
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 11

Persists versioned workflow definitions and execution run instances to the
authoritative runtime SQLite database using ACID transactions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.storage.database import transaction
from src.trust.audit import _check_no_secrets
from src.workflow.models import (
    WorkflowBudget,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepDefinition,
    WorkflowStepExecutionRecord,
)

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _sanitize_and_serialize_json(data: Dict[str, Any]) -> str:
    """Serialize dictionary to JSON while verifying zero raw secrets."""
    serialized = json.dumps(data, sort_keys=True)
    _check_no_secrets(serialized)
    return serialized


class WorkflowStore:
    """SQLite-backed transactional persistence layer for workflows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize store with an active SQLite connection."""
        self._conn = conn

    # ==========================================================================
    # WORKFLOW DEFINITIONS
    # ==========================================================================

    def save_definition(self, defn: WorkflowDefinition) -> WorkflowDefinition:
        """Persist or update a workflow definition.

        Args:
            defn: WorkflowDefinition to save.

        Returns:
            The saved WorkflowDefinition.
        """
        dag_dict = {
            "steps": [s.model_dump() for s in defn.steps],
            "budget": defn.budget.model_dump(),
            "metadata": defn.metadata,
        }
        dag_json = _sanitize_and_serialize_json(dag_dict)

        sql = (
            "INSERT INTO workflow_definitions "
            "(workflow_id, version, project_id, name, description, is_active, "
            " dag_specification_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workflow_id, version) DO UPDATE SET "
            "  project_id = excluded.project_id, "
            "  name = excluded.name, "
            "  description = excluded.description, "
            "  is_active = excluded.is_active, "
            "  dag_specification_json = excluded.dag_specification_json, "
            "  updated_at = excluded.updated_at;"
        )

        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(
                sql,
                (
                    defn.workflow_id,
                    defn.version,
                    defn.project_id,
                    defn.name,
                    defn.description,
                    1 if defn.is_active else 0,
                    dag_json,
                    defn.created_at,
                    defn.updated_at,
                ),
            )

        return defn

    def get_definition(
        self,
        workflow_id: str,
        version: Optional[int] = None,
    ) -> Optional[WorkflowDefinition]:
        """Retrieve a workflow definition by ID and optional version.

        If version is None, retrieves the highest available version.
        """
        if version is not None:
            sql = (
                "SELECT workflow_id, version, project_id, name, description, "
                "       is_active, dag_specification_json, created_at, updated_at "
                "FROM workflow_definitions "
                "WHERE workflow_id = ? AND version = ?;"
            )
            row = self._conn.execute(sql, (workflow_id, version)).fetchone()
        else:
            sql = (
                "SELECT workflow_id, version, project_id, name, description, "
                "       is_active, dag_specification_json, created_at, updated_at "
                "FROM workflow_definitions "
                "WHERE workflow_id = ? "
                "ORDER BY version DESC LIMIT 1;"
            )
            row = self._conn.execute(sql, (workflow_id,)).fetchone()

        if row is None:
            return None

        dag_spec = json.loads(row["dag_specification_json"])
        steps = [WorkflowStepDefinition(**s) for s in dag_spec.get("steps", [])]
        budget = WorkflowBudget(**dag_spec.get("budget", {}))
        metadata = dag_spec.get("metadata", {})

        return WorkflowDefinition(
            workflow_id=row["workflow_id"],
            version=row["version"],
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            is_active=bool(row["is_active"]),
            steps=steps,
            budget=budget,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=metadata,
        )

    def list_definitions(
        self,
        project_id: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[WorkflowDefinition]:
        """List workflow definitions, optionally filtered by project and active status."""
        conditions = []
        params: List[Any] = []

        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if is_active is not None:
            conditions.append("is_active = ?")
            params.append(1 if is_active else 0)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT workflow_id, version, project_id, name, description, "
            f"       is_active, dag_specification_json, created_at, updated_at "
            f"FROM workflow_definitions {where_clause} "
            f"ORDER BY workflow_id ASC, version DESC;"
        )

        rows = self._conn.execute(sql, params).fetchall()
        definitions: List[WorkflowDefinition] = []
        for row in rows:
            dag_spec = json.loads(row["dag_specification_json"])
            steps = [WorkflowStepDefinition(**s) for s in dag_spec.get("steps", [])]
            budget = WorkflowBudget(**dag_spec.get("budget", {}))
            metadata = dag_spec.get("metadata", {})

            definitions.append(
                WorkflowDefinition(
                    workflow_id=row["workflow_id"],
                    version=row["version"],
                    project_id=row["project_id"],
                    name=row["name"],
                    description=row["description"],
                    is_active=bool(row["is_active"]),
                    steps=steps,
                    budget=budget,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    metadata=metadata,
                )
            )

        return definitions

    # ==========================================================================
    # WORKFLOW RUNS (INSTANCES)
    # ==========================================================================

    def create_run(self, run: WorkflowRun) -> WorkflowRun:
        """Persist a new workflow execution run."""
        inputs_json = _sanitize_and_serialize_json(run.inputs)
        outputs_json = _sanitize_and_serialize_json(run.outputs)
        step_states_dict = {k: v.model_dump() for k, v in run.step_states.items()}
        step_states_json = _sanitize_and_serialize_json(step_states_dict)

        sql = (
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, workflow_version, project_id, task_id, "
            " status, triggered_by, inputs_json, outputs_json, step_states_json, "
            " error_summary, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
        )

        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(
                sql,
                (
                    run.run_id,
                    run.workflow_id,
                    run.workflow_version,
                    run.project_id,
                    run.task_id,
                    run.status.value,
                    run.triggered_by,
                    inputs_json,
                    outputs_json,
                    step_states_json,
                    run.error_summary,
                    run.started_at,
                    run.completed_at,
                ),
            )

        return run

    def update_run(self, run: WorkflowRun) -> WorkflowRun:
        """Update the state and output of an existing workflow run."""
        outputs_json = _sanitize_and_serialize_json(run.outputs)
        step_states_dict = {k: v.model_dump() for k, v in run.step_states.items()}
        step_states_json = _sanitize_and_serialize_json(step_states_dict)

        sql = (
            "UPDATE workflow_runs SET "
            "  status = ?, "
            "  task_id = ?, "
            "  outputs_json = ?, "
            "  step_states_json = ?, "
            "  error_summary = ?, "
            "  completed_at = ? "
            "WHERE run_id = ?;"
        )

        with transaction(self._conn, immediate=True) as cursor:
            cursor.execute(
                sql,
                (
                    run.status.value,
                    run.task_id,
                    outputs_json,
                    step_states_json,
                    run.error_summary,
                    run.completed_at,
                    run.run_id,
                ),
            )

        return run

    def get_run(self, run_id: str) -> Optional[WorkflowRun]:
        """Fetch a workflow run by run_id."""
        sql = (
            "SELECT run_id, workflow_id, workflow_version, project_id, task_id, "
            "       status, triggered_by, inputs_json, outputs_json, step_states_json, "
            "       error_summary, started_at, completed_at "
            "FROM workflow_runs "
            "WHERE run_id = ?;"
        )
        row = self._conn.execute(sql, (run_id,)).fetchone()
        if row is None:
            return None

        inputs = json.loads(row["inputs_json"])
        outputs = json.loads(row["outputs_json"])
        step_states_raw = json.loads(row["step_states_json"])
        step_states = {k: WorkflowStepExecutionRecord(**v) for k, v in step_states_raw.items()}

        return WorkflowRun(
            run_id=row["run_id"],
            workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            status=WorkflowStatus(row["status"]),
            triggered_by=row["triggered_by"],
            inputs=inputs,
            outputs=outputs,
            step_states=step_states,
            error_summary=row["error_summary"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def list_runs(
        self,
        workflow_id: Optional[str] = None,
        project_id: Optional[str] = None,
        status: Optional[WorkflowStatus] = None,
    ) -> List[WorkflowRun]:
        """List workflow runs with optional filters."""
        conditions = []
        params: List[Any] = []

        if workflow_id is not None:
            conditions.append("workflow_id = ?")
            params.append(workflow_id)
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT run_id, workflow_id, workflow_version, project_id, task_id, "
            f"       status, triggered_by, inputs_json, outputs_json, step_states_json, "
            f"       error_summary, started_at, completed_at "
            f"FROM workflow_runs {where_clause} "
            f"ORDER BY started_at DESC;"
        )

        rows = self._conn.execute(sql, params).fetchall()
        runs: List[WorkflowRun] = []
        for row in rows:
            inputs = json.loads(row["inputs_json"])
            outputs = json.loads(row["outputs_json"])
            step_states_raw = json.loads(row["step_states_json"])
            step_states = {k: WorkflowStepExecutionRecord(**v) for k, v in step_states_raw.items()}
            runs.append(
                WorkflowRun(
                    run_id=row["run_id"],
                    workflow_id=row["workflow_id"],
                    workflow_version=row["workflow_version"],
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    status=WorkflowStatus(row["status"]),
                    triggered_by=row["triggered_by"],
                    inputs=inputs,
                    outputs=outputs,
                    step_states=step_states,
                    error_summary=row["error_summary"],
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                )
            )

        return runs
