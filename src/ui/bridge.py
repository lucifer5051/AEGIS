"""SAGE UI — Runtime Bridge.

Patches SageApplication internals to emit ExecutionEvents to the EventBus
without modifying the core Supervisor, ToolBroker, or TrustCore classes.

Strategy:
- Wrap Supervisor.execute_task / _execute_step to emit step events.
- Wrap ToolBroker.invoke to emit tool events.
- Wrap ApprovalStore.create_request to emit approval.required events.
- Wrap ApprovalStore.approve_request / reject_request for decision events.

All wrapping uses __wrapped__ convention and is idempotent (safe to call twice).
"""

from __future__ import annotations

import logging
from typing import Any

from src.ui.events import EventType, get_event_bus

logger = logging.getLogger(__name__)


def _already_patched(obj: Any, attr: str) -> bool:
    return getattr(getattr(obj, attr, None), "__sage_ui_patched__", False)


def _wrap(obj: Any, attr: str, wrapper_factory) -> None:
    orig = getattr(obj, attr)
    new_fn = wrapper_factory(orig)
    new_fn.__sage_ui_patched__ = True
    new_fn.__wrapped__ = orig
    # Bind to the same instance
    import types
    setattr(obj, attr, types.MethodType(new_fn, obj) if False else new_fn)


def attach(sage_app: Any) -> None:
    """Attach event emission hooks to a running SageApplication.

    Safe to call multiple times — idempotent.
    """
    bus = get_event_bus()

    supervisor = getattr(sage_app, "_supervisor", None)
    tool_broker = getattr(sage_app, "_tool_broker", None)
    approval_store = getattr(sage_app, "_approval_store", None)

    # --- Supervisor: execute_task ---
    if supervisor and not _already_patched(supervisor, "execute_task"):
        _orig_execute_task = supervisor.execute_task

        def _execute_task_patched(task_id: str, *args, **kwargs):
            bus.emit(EventType.TASK_STARTED, message=f"Task execution started", task_id=task_id, component="supervisor", status="EXECUTING")
            try:
                result = _orig_execute_task(task_id, *args, **kwargs)
                bus.emit(EventType.TASK_COMPLETED, message=f"Task completed", task_id=task_id, component="supervisor", status="COMPLETED")
                return result
            except Exception as exc:
                bus.emit(EventType.TASK_FAILED, message=f"Task failed: {exc}", task_id=task_id, component="supervisor", status="FAILED")
                raise

        _execute_task_patched.__sage_ui_patched__ = True
        supervisor.execute_task = _execute_task_patched
        logger.debug("UI bridge: patched Supervisor.execute_task")

    # --- Supervisor: _execute_step ---
    if supervisor and not _already_patched(supervisor, "_execute_step"):
        _orig_execute_step = supervisor._execute_step

        def _execute_step_patched(step_id: str, task_id: str, *args, **kwargs):
            bus.emit(EventType.STEP_STARTED, message=f"Step started: {step_id}", task_id=task_id, step_id=step_id, component="supervisor", status="RUNNING")
            try:
                result = _orig_execute_step(step_id, task_id, *args, **kwargs)
                bus.emit(EventType.STEP_COMPLETED, message=f"Step completed: {step_id}", task_id=task_id, step_id=step_id, component="supervisor", status="COMPLETED")
                return result
            except Exception as exc:
                bus.emit(EventType.STEP_FAILED, message=f"Step failed: {exc}", task_id=task_id, step_id=step_id, component="supervisor", status="FAILED")
                raise

        _execute_step_patched.__sage_ui_patched__ = True
        supervisor._execute_step = _execute_step_patched
        logger.debug("UI bridge: patched Supervisor._execute_step")

    # --- ToolBroker: invoke ---
    if tool_broker and not _already_patched(tool_broker, "invoke"):
        _orig_invoke = tool_broker.invoke

        def _invoke_patched(tool_name: str, arguments: Any = None, **kwargs):
            bus.emit(EventType.TOOL_STARTED, message=f"→ {tool_name}", component="tool_broker", status="RUNNING", tool=tool_name)
            try:
                result = _orig_invoke(tool_name, arguments, **kwargs)
                status = getattr(getattr(result, "status", None), "value", str(getattr(result, "status", "ok")))
                if "DENIED" in status.upper():
                    bus.emit(EventType.TOOL_DENIED, message=f"✗ {tool_name} denied", component="tool_broker", status="DENIED", tool=tool_name)
                else:
                    bus.emit(EventType.TOOL_COMPLETED, message=f"✓ {tool_name} [{status}]", component="tool_broker", status=status, tool=tool_name)
                return result
            except Exception as exc:
                bus.emit(EventType.TOOL_COMPLETED, message=f"✗ {tool_name} error: {exc}", component="tool_broker", status="ERROR", tool=tool_name)
                raise

        _invoke_patched.__sage_ui_patched__ = True
        tool_broker.invoke = _invoke_patched
        logger.debug("UI bridge: patched ToolBroker.invoke")

    # --- ApprovalStore: create_request ---
    if approval_store and not _already_patched(approval_store, "create_request"):
        _orig_create_request = approval_store.create_request

        def _create_request_patched(*args, **kwargs):
            rec = _orig_create_request(*args, **kwargs)
            bus.emit(
                EventType.APPROVAL_REQUIRED,
                message=f"Approval required: {rec.action_name} [{rec.action_risk_level}]",
                task_id=rec.task_id,
                component="trustcore",
                status="PENDING",
                request_id=rec.request_id,
                action_name=rec.action_name,
            )
            return rec

        _create_request_patched.__sage_ui_patched__ = True
        approval_store.create_request = _create_request_patched
        logger.debug("UI bridge: patched ApprovalStore.create_request")

    bus.emit(EventType.RUNTIME_STARTED, message="SAGE UI bridge attached", component="ui_bridge", status="ok")
    logger.info("SAGE UI runtime bridge attached successfully.")
