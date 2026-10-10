"""SAGE Durable Workflow Engine -- Dependency-Aware DAG Execution Engine.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 11
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 11

Coordinates persistent, dependency-aware DAG execution through the secured pipeline:
Supervisor → Skill Registry → Admission Controller → BoundSkillToolBroker / ToolBroker
→ Trust Core → Step Verifier → Checkpoints → Structured Results.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from src.manager.models import TaskState
from src.manager.observation import StepObservation
from src.manager.planner import PlannedStep
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.manager.verifier import StepVerifier, VerificationOutcome
from src.skills.models import SkillInvocation
from src.tools.broker import ToolBroker
from src.tools.models import ToolResultStatus
from src.trust.authority import TrustAuthority
from src.workflow.errors import (
    WorkflowNotFoundError,
    WorkflowStateError,
)
from src.workflow.models import (
    FailurePolicy,
    VerificationPolicy,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepDefinition,
    WorkflowStepExecutionRecord,
    WorkflowStepStatus,
)
from src.workflow.store import WorkflowStore
from src.workflow.validator import validate_workflow_definition

logger = logging.getLogger(__name__)

_TEMPLATE_REF_REGEX = re.compile(
    r"\{\{\s*(?:steps\.([a-zA-Z0-9_\-]+)\.(?:output|outputs)\.([a-zA-Z0-9_\-]+)|inputs\.([a-zA-Z0-9_\-]+))\s*\}\}"
)


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _interpolate_value(
    val: Any,
    inputs: Dict[str, Any],
    step_outputs: Dict[str, Dict[str, Any]],
) -> Any:
    """Interpolate template references like {{inputs.key}} or {{steps.s1.output.key}}."""
    if isinstance(val, str):
        # Full match check for exact non-string replacement (e.g. dict or list)
        full_match = _TEMPLATE_REF_REGEX.fullmatch(val.strip())
        if full_match:
            step_id, output_key, input_key = full_match.groups()
            if input_key:
                return inputs.get(input_key, "")
            if step_id and output_key:
                step_out = step_outputs.get(step_id, {})
                return step_out.get(output_key, "")

        def _repl(m: re.Match[str]) -> str:
            step_id, output_key, input_key = m.groups()
            if input_key:
                res = inputs.get(input_key, "")
                return str(res)
            if step_id and output_key:
                step_out = step_outputs.get(step_id, {})
                res = step_out.get(output_key, "")
                return str(res)
            return m.group(0)

        return _TEMPLATE_REF_REGEX.sub(_repl, val)
    elif isinstance(val, dict):
        return {k: _interpolate_value(v, inputs, step_outputs) for k, v in val.items()}
    elif isinstance(val, list):
        return [_interpolate_value(item, inputs, step_outputs) for item in val]
    return val


class WorkflowEngine:
    """Executes versioned, persistent DAG workflows with dependency tracking and checkpointing."""

    def __init__(
        self,
        workflow_store: WorkflowStore,
        state_store: StateStore,
        supervisor: Supervisor,
        trust_authority: TrustAuthority,
        tool_broker: ToolBroker,
        verifier: Optional[StepVerifier] = None,
        skill_registry: Optional[Any] = None,
        admission_controller: Optional[Any] = None,
    ) -> None:
        """Initialize workflow engine with required subsystems."""
        self._store = workflow_store
        self._state_store = state_store
        self._supervisor = supervisor
        self._authority = trust_authority
        self._tools = tool_broker
        self._verifier = verifier or StepVerifier()
        self._skill_registry = skill_registry
        self._admission = admission_controller

    # ==========================================================================
    # WORKFLOW LIFECYCLE
    # ==========================================================================

    def start_workflow(
        self,
        workflow_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        actor: str = "user",
        project_id: Optional[str] = None,
        version: Optional[int] = None,
        triggered_by: str = "user",
    ) -> WorkflowRun:
        """Instantiate and start execution of a workflow.

        Validates the workflow definition, records initial states and task records,
        and executes eligible steps deterministically.
        """
        defn = self._store.get_definition(workflow_id, version=version)
        if defn is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' (version={version}) not found.")

        validate_workflow_definition(
            defn,
            skill_registry=self._skill_registry,
            tool_broker=self._tools,
        )

        wf_inputs = dict(inputs or {})

        # 1. Backing task in StateStore (reuse existing task schema)
        task_record = self._state_store.create_task(
            title=f"Workflow: {defn.name} (v{defn.version})",
            project_id=project_id,
            routing_intent="workflow_execution",
            priority=100,
            metadata={
                "workflow_id": defn.workflow_id,
                "workflow_version": defn.version,
                "triggered_by": triggered_by,
                "actor": actor,
            },
        )
        self._state_store.transition_task_state(task_record.task_id, TaskState.PLANNING)
        self._state_store.transition_task_state(task_record.task_id, TaskState.EXECUTING)

        # 2. Convert workflow steps to task steps in StateStore
        for idx, s in enumerate(defn.steps, start=1):
            self._state_store.create_step(
                task_id=task_record.task_id,
                step_index=idx,
                action_name=s.action_name or s.tool_name or s.skill_id or s.step_id,
                step_id=s.step_id,
                tool_name=s.tool_name,
                is_idempotent=s.is_idempotent,
                idempotency_key=s.idempotency_key or f"{task_record.task_id}:{s.step_id}",
                arguments=dict(s.arguments),
            )

        # 3. Create initial WorkflowRun
        now = _now_utc()
        run = WorkflowRun(
            run_id=str(uuid4()),
            workflow_id=defn.workflow_id,
            workflow_version=defn.version,
            project_id=project_id,
            task_id=task_record.task_id,
            status=WorkflowStatus.RUNNING,
            triggered_by=triggered_by,
            inputs=wf_inputs,
            outputs={},
            step_states={
                s.step_id: WorkflowStepExecutionRecord(
                    step_id=s.step_id,
                    status=WorkflowStepStatus.PENDING,
                )
                for s in defn.steps
            },
            started_at=now,
        )
        self._store.create_run(run)

        # 4. Execute steps
        return self._execute_run(run, defn, actor=actor)

    def pause_workflow(self, run_id: str) -> WorkflowRun:
        """Pause an in-progress workflow run."""
        run = self._store.get_run(run_id)
        if run is None:
            raise WorkflowNotFoundError(f"Workflow run '{run_id}' not found.")

        if not run.status.can_transition_to(WorkflowStatus.PAUSED):
            raise WorkflowStateError(f"Cannot pause workflow run in state '{run.status.value}'.")

        updated_run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=WorkflowStatus.PAUSED,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=run.outputs,
            step_states=run.step_states,
            error_summary=run.error_summary,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )
        return self._store.update_run(updated_run)

    def resume_workflow(
        self,
        run_id: str,
        actor: str = "user",
    ) -> WorkflowRun:
        """Resume execution of a paused or recovered workflow run."""
        run = self._store.get_run(run_id)
        if run is None:
            raise WorkflowNotFoundError(f"Workflow run '{run_id}' not found.")

        if not run.status.can_transition_to(WorkflowStatus.RUNNING):
            raise WorkflowStateError(f"Cannot resume workflow run from state '{run.status.value}'.")

        defn = self._store.get_definition(run.workflow_id, version=run.workflow_version)
        if defn is None:
            raise WorkflowNotFoundError(
                f"Workflow definition '{run.workflow_id}' (v{run.workflow_version}) not found."
            )

        # Transition status to RUNNING
        updated_run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=WorkflowStatus.RUNNING,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=run.outputs,
            step_states=run.step_states,
            error_summary=None,
            started_at=run.started_at,
            completed_at=None,
        )
        self._store.update_run(updated_run)

        return self._execute_run(updated_run, defn, actor=actor)

    def cancel_workflow(
        self,
        run_id: str,
        reason: str = "Cancelled by user",
    ) -> WorkflowRun:
        """Cancel an in-progress or paused workflow run."""
        run = self._store.get_run(run_id)
        if run is None:
            raise WorkflowNotFoundError(f"Workflow run '{run_id}' not found.")

        if not run.status.can_transition_to(WorkflowStatus.CANCELLED):
            raise WorkflowStateError(
                f"Cannot cancel workflow run in terminal state '{run.status.value}'."
            )

        now = _now_utc()
        updated_run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=WorkflowStatus.CANCELLED,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=run.outputs,
            step_states=run.step_states,
            error_summary=reason,
            started_at=run.started_at,
            completed_at=now,
        )
        self._store.update_run(updated_run)

        if run.task_id:
            try:
                self._state_store.transition_task_state(run.task_id, TaskState.CANCELLED)
            except Exception:
                pass

        return updated_run

    # ==========================================================================
    # EXECUTION LOOP
    # ==========================================================================

    def _execute_run(
        self,
        run: WorkflowRun,
        defn: WorkflowDefinition,
        actor: str,
    ) -> WorkflowRun:
        """Internal dependency-aware step execution loop."""
        start_mono = time.monotonic()
        total_retries = sum(sr.retry_count for sr in run.step_states.values())

        step_outputs: Dict[str, Dict[str, Any]] = {
            sid: dict(sr.outputs) for sid, sr in run.step_states.items()
        }

        # Step lookup for task steps
        task_step_records = {}
        if run.task_id:
            try:
                task_step_records = {
                    ts.action_name: ts for ts in self._state_store.list_task_steps(run.task_id)
                }
            except Exception:
                task_step_records = {}

        while True:
            # Check if execution paused or cancelled externally
            current_state = self._store.get_run(run.run_id)
            if current_state and current_state.status != WorkflowStatus.RUNNING:
                return current_state

            # 1. Budget and Deadline Validation
            if defn.budget.max_execution_seconds is not None:
                elapsed = time.monotonic() - start_mono
                if elapsed >= defn.budget.max_execution_seconds:
                    err_msg = (
                        f"Workflow budget exceeded: execution elapsed {elapsed:.1f}s "
                        f"(limit={defn.budget.max_execution_seconds}s)."
                    )
                    return self._fail_run(run, err_msg)

            if total_retries > defn.budget.max_total_retries:
                err_msg = (
                    f"Workflow budget exceeded: total retries {total_retries} "
                    f"exceeded limit of {defn.budget.max_total_retries}."
                )
                return self._fail_run(run, err_msg)

            # 2. Check for eligible steps
            eligible_step: Optional[WorkflowStepDefinition] = None
            all_done = True

            for step in defn.steps:
                state = run.step_states[step.step_id]
                if state.status == WorkflowStepStatus.COMPLETED:
                    continue
                if state.status in (WorkflowStepStatus.FAILED, WorkflowStepStatus.SKIPPED):
                    continue

                all_done = False

                # Check if all dependencies are COMPLETED
                deps_satisfied = True
                for dep_id in step.dependencies:
                    dep_state = run.step_states.get(dep_id)
                    if dep_state is None or dep_state.status != WorkflowStepStatus.COMPLETED:
                        deps_satisfied = False
                        break

                if deps_satisfied and state.status in (
                    WorkflowStepStatus.PENDING,
                    WorkflowStepStatus.RUNNING,
                ):
                    eligible_step = step
                    break

            if all_done:
                # All steps resolved! Complete workflow
                return self._complete_run(run, step_outputs)

            if eligible_step is None:
                # No step is eligible, but not all done -> Check for blocked/failed states
                failed_or_skipped = any(
                    sr.status in (WorkflowStepStatus.FAILED, WorkflowStepStatus.AWAITING_APPROVAL)
                    for sr in run.step_states.values()
                )
                if failed_or_skipped:
                    awaiting = any(
                        sr.status == WorkflowStepStatus.AWAITING_APPROVAL
                        for sr in run.step_states.values()
                    )
                    if awaiting:
                        return self.pause_workflow(run.run_id)
                    return self._fail_run(run, "Workflow halted due to dependency failure.")
                # If neither eligible nor failed, we reached deadlock
                return self._fail_run(run, "No eligible steps found (unmet dependencies).")

            # 3. Execute Eligible Step
            run, step_success, step_out, retries_added = self._execute_step(
                run=run,
                step=eligible_step,
                inputs=run.inputs,
                step_outputs=step_outputs,
                actor=actor,
                task_step_records=task_step_records,
            )
            total_retries += retries_added

            if step_success:
                step_outputs[eligible_step.step_id] = step_out
            else:
                if run.status == WorkflowStatus.PAUSED:
                    return run
                # Step failed and could not recover
                if eligible_step.failure_policy == FailurePolicy.STOP:
                    return self._fail_run(
                        run,
                        f"Step '{eligible_step.step_id}' failed: "
                        f"{run.step_states[eligible_step.step_id].error}",
                    )
                elif eligible_step.failure_policy == FailurePolicy.IGNORE_AND_CONTINUE:
                    logger.warning(
                        "Step '%s' failed but failure_policy is IGNORE_AND_CONTINUE; continuing.",
                        eligible_step.step_id,
                    )

    def _execute_step(
        self,
        run: WorkflowRun,
        step: WorkflowStepDefinition,
        inputs: Dict[str, Any],
        step_outputs: Dict[str, Dict[str, Any]],
        actor: str,
        task_step_records: Dict[str, Any],
    ) -> tuple[WorkflowRun, bool, Dict[str, Any], int]:
        """Execute a single workflow step with verification, bounded retries, and checkpoints."""
        step_record = run.step_states[step.step_id]
        retries_added = 0
        retry_count = step_record.retry_count

        # Mark RUNNING
        updated_states = dict(run.step_states)
        updated_states[step.step_id] = WorkflowStepExecutionRecord(
            step_id=step.step_id,
            status=WorkflowStepStatus.RUNNING,
            retry_count=retry_count,
            inputs=dict(step.arguments),
            outputs=step_record.outputs,
            started_at=_now_utc(),
        )
        run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=run.status,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=run.outputs,
            step_states=updated_states,
            started_at=run.started_at,
        )
        self._store.update_run(run)

        # Interpolate arguments
        resolved_args = _interpolate_value(step.arguments, inputs, step_outputs)
        if not isinstance(resolved_args, dict):
            resolved_args = {"value": resolved_args}

        while True:
            # Revalidate authorization / capability before dispatch
            try:
                # Execution dispatch: Skill vs Tool
                if step.skill_id and self._supervisor:
                    invocation = SkillInvocation(
                        invocation_id=str(uuid4()),
                        skill_id=step.skill_id,
                        input=resolved_args,
                    )
                    skill_result = self._supervisor.execute_skill(
                        invocation,
                        actor=actor,
                        project_id=run.project_id,
                    )

                    is_verified = (
                        skill_result.verification_status.value == "VERIFIED_SUCCESS"
                        if hasattr(skill_result, "verification_status")
                        else skill_result.status.value == "SUCCEEDED"
                    )

                    if is_verified:
                        output_dict = (
                            skill_result.output
                            if isinstance(skill_result.output, dict)
                            else {"result": skill_result.output}
                        )
                        return self._complete_step(
                            run, step, resolved_args, output_dict, retry_count, retries_added
                        )

                    error_msg = skill_result.error or "Skill verification failed."
                    needs_approval = (
                        getattr(skill_result, "requires_approval", False)
                        or "approval" in error_msg.lower()
                    )

                elif step.tool_name:
                    tool_result = self._tools.invoke(
                        tool_name=step.tool_name,
                        args=resolved_args,
                        actor=actor,
                        project_id=run.project_id,
                    )

                    # VERIFICATION: A tool response alone NEVER marks a step complete!
                    planned_step = PlannedStep(
                        step_number=1,
                        tool=step.tool_name,
                        arguments=resolved_args,
                        description=step.title,
                        is_idempotent=step.is_idempotent,
                        acceptance_criteria=f"Tool {step.tool_name} executed successfully.",
                    )
                    obs = StepObservation.from_tool_result(
                        step_number=1,
                        tool_name=step.tool_name,
                        result=tool_result,
                    )

                    if step.verification_policy == VerificationPolicy.NONE:
                        is_verified = tool_result.status == ToolResultStatus.SUCCESS
                        verification_rationale = "Verification bypassed by policy NONE."
                        needs_approval = False
                    else:
                        ver = self._verifier.verify(planned_step, obs)
                        is_verified = ver.outcome == VerificationOutcome.VERIFIED_SUCCESS
                        verification_rationale = ver.rationale
                        needs_approval = ver.requires_approval

                    if is_verified:
                        output_dict = {"output": tool_result.output}
                        return self._complete_step(
                            run, step, resolved_args, output_dict, retry_count, retries_added
                        )

                    error_msg = verification_rationale

                else:
                    # Action name or passthrough computation
                    output_dict = {"status": "ok", "inputs": resolved_args}
                    return self._complete_step(
                        run, step, resolved_args, output_dict, retry_count, retries_added
                    )

            except Exception as exc:
                error_msg = str(exc)
                needs_approval = "approval" in error_msg.lower()

            # Handle Approval Requirement
            if needs_approval:
                updated_states = dict(run.step_states)
                updated_states[step.step_id] = WorkflowStepExecutionRecord(
                    step_id=step.step_id,
                    status=WorkflowStepStatus.AWAITING_APPROVAL,
                    retry_count=retry_count,
                    inputs=resolved_args,
                    error=error_msg,
                    started_at=run.step_states[step.step_id].started_at,
                )
                run = WorkflowRun(
                    run_id=run.run_id,
                    workflow_id=run.workflow_id,
                    workflow_version=run.workflow_version,
                    project_id=run.project_id,
                    task_id=run.task_id,
                    status=WorkflowStatus.PAUSED,
                    triggered_by=run.triggered_by,
                    inputs=run.inputs,
                    outputs=run.outputs,
                    step_states=updated_states,
                    error_summary=f"Step '{step.step_id}' requires human approval.",
                    started_at=run.started_at,
                )
                self._store.update_run(run)
                return run, False, {}, retries_added

            # Idempotency and Bounded Retries
            if step.is_idempotent and retry_count < step.max_retries:
                retry_count += 1
                retries_added += 1
                logger.warning(
                    "Retrying idempotent step '%s' (attempt %d/%d): %s",
                    step.step_id,
                    retry_count,
                    step.max_retries,
                    error_msg,
                )
                continue

            # Non-retryable or max retries exhausted
            break

        # Step Failed
        updated_states = dict(run.step_states)
        updated_states[step.step_id] = WorkflowStepExecutionRecord(
            step_id=step.step_id,
            status=WorkflowStepStatus.FAILED,
            retry_count=retry_count,
            inputs=resolved_args,
            error=error_msg,
            started_at=run.step_states[step.step_id].started_at,
            completed_at=_now_utc(),
        )
        run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=run.status,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=run.outputs,
            step_states=updated_states,
            started_at=run.started_at,
        )
        self._store.update_run(run)
        return run, False, {}, retries_added

    def _complete_step(
        self,
        run: WorkflowRun,
        step: WorkflowStepDefinition,
        inputs: Dict[str, Any],
        outputs: Dict[str, Any],
        retry_count: int,
        retries_added: int,
    ) -> tuple[WorkflowRun, bool, Dict[str, Any], int]:
        """Record step completion and checkpoint to StateStore."""
        now = _now_utc()
        updated_states = dict(run.step_states)
        updated_states[step.step_id] = WorkflowStepExecutionRecord(
            step_id=step.step_id,
            status=WorkflowStepStatus.COMPLETED,
            retry_count=retry_count,
            inputs=inputs,
            outputs=outputs,
            started_at=run.step_states[step.step_id].started_at,
            completed_at=now,
        )

        # Durably checkpoint in StateStore (reuse task_checkpoints schema)
        if run.task_id:
            try:
                existing_checkpoints = self._state_store.list_checkpoints(run.task_id)
                seq = len(existing_checkpoints) + 1
                self._state_store.create_checkpoint(
                    task_id=run.task_id,
                    checkpoint_seq=seq,
                    step_id=step.step_id,
                    snapshot={
                        "run_id": run.run_id,
                        "step_id": step.step_id,
                        "completed_step_outputs": outputs,
                        "timestamp": now,
                    },
                )
            except Exception as exc:
                logger.warning("Checkpoint save warning for task %s: %s", run.task_id, exc)

        run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=run.status,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=run.outputs,
            step_states=updated_states,
            started_at=run.started_at,
        )
        self._store.update_run(run)
        return run, True, outputs, retries_added

    def _complete_run(
        self,
        run: WorkflowRun,
        step_outputs: Dict[str, Dict[str, Any]],
    ) -> WorkflowRun:
        """Mark workflow run as COMPLETED with aggregated output."""
        now = _now_utc()
        # Consolidate outputs from terminal steps
        final_outputs: Dict[str, Any] = {}
        for sid, outs in step_outputs.items():
            final_outputs[sid] = outs

        completed_run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=WorkflowStatus.COMPLETED,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=final_outputs,
            step_states=run.step_states,
            error_summary=None,
            started_at=run.started_at,
            completed_at=now,
        )
        self._store.update_run(completed_run)

        if run.task_id:
            try:
                self._state_store.transition_task_state(run.task_id, TaskState.COMPLETED)
            except Exception:
                pass

        return completed_run

    def _fail_run(
        self,
        run: WorkflowRun,
        error_summary: str,
    ) -> WorkflowRun:
        """Mark workflow run as FAILED."""
        now = _now_utc()
        failed_run = WorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_version=run.workflow_version,
            project_id=run.project_id,
            task_id=run.task_id,
            status=WorkflowStatus.FAILED,
            triggered_by=run.triggered_by,
            inputs=run.inputs,
            outputs=run.outputs,
            step_states=run.step_states,
            error_summary=error_summary,
            started_at=run.started_at,
            completed_at=now,
        )
        self._store.update_run(failed_run)

        if run.task_id:
            try:
                self._state_store.transition_task_state(
                    run.task_id,
                    TaskState.FAILED,
                    error_summary=error_summary,
                )
            except Exception:
                pass

        return failed_run
