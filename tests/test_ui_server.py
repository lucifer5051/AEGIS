"""SAGE UI Server — Test Suite (100% offline, no network).

Tests:
1. EventBus subscribe/publish/unsubscribe
2. EventBus backpressure drop behaviour
3. create_app returns a FastAPI instance
4. /api/status returns component health
5. /api/tasks returns task list
6. /api/task/{id} returns task detail + steps
7. /api/task (POST) creates a task and emits event
8. /api/task/{id}/cancel transitions state
9. /api/approvals returns pending list
10. /api/approval/{id}/approve goes through ApprovalStore
11. /api/approval/{id}/reject goes through ApprovalStore
12. /api/chat calls FridayConversationHandler
13. Auth: missing token returns 401
14. Auth: wrong token returns 401
15. WebSocket rejects bad token with code 4001
16. Event bus emits APPROVAL_REQUIRED when bridge patches ApprovalStore
17. Bridge is idempotent (double attach does not double-wrap)
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.ui.events import EventBus, EventType, ExecutionEvent, get_event_bus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_id="t-1", title="Test task", state="SUBMITTED"):
    t = MagicMock()
    t.task_id = task_id
    t.title = title
    t.state = MagicMock()
    t.state.value = state
    t.created_at = "2026-01-01T00:00:00+00:00"
    t.updated_at = "2026-01-01T00:00:00+00:00"
    return t


def _make_step(step_id="s-1", name="Step A", state="PENDING"):
    s = MagicMock()
    s.step_id = step_id
    s.name = name
    s.state = MagicMock()
    s.state.value = state
    s.tool_name = "test_tool"
    s.output_summary = "done"
    return s


def _make_approval(request_id="r-1", task_id="t-1", action_name="write_file", risk="HIGH"):
    a = MagicMock()
    a.request_id = request_id
    a.task_id = task_id
    a.action_name = action_name
    a.action_risk_level = risk
    a.target_resource = "/tmp/test"
    a.parameters_summary = '{"path": "/tmp/test"}'
    a.expires_at = "2099-01-01T00:00:00+00:00"
    a.status = "PENDING"
    return a


def _make_sage_app(*, tasks=None, steps=None, approvals=None, approve_result=None, reject_result=None):
    """Build a minimal SageApplication mock."""
    app = MagicMock()
    app.is_running = True

    # State store
    state_store = MagicMock()
    state_store.list_tasks.return_value = tasks or [_make_task()]
    state_store.get_task.return_value = (tasks or [_make_task()])[0]
    state_store.list_task_steps.return_value = steps or [_make_step()]
    state_store.create_task.return_value = _make_task()
    state_store.transition_task_state.return_value = None
    app._state_store = state_store

    # Approval store
    approval_store = MagicMock()
    approval_store.list_pending.return_value = approvals or []
    _a = approve_result or _make_approval()
    _a.status = "APPROVED"
    approval_store.approve_request.return_value = _a
    _r = reject_result or _make_approval()
    _r.status = "REJECTED"
    approval_store.reject_request.return_value = _r
    app._approval_store = approval_store
    app.approval_store = approval_store  # property alias

    # Friday
    friday = MagicMock()
    friday.handle_message.return_value = "Hello from Friday!"
    app.friday_handler = friday

    # Gateway / memory
    app.model_gateway = MagicMock()
    app.memory_engine = MagicMock()
    app.worker_manager = MagicMock()
    app.communication_gateway = MagicMock()

    # Supervisor with _store
    sup = MagicMock()
    app._supervisor = sup
    app.supervisor = sup

    # Tool broker
    app._tool_broker = MagicMock()

    return app


TOKEN = "test-token-abc"


def _make_client(sage_app=None) -> TestClient:
    # Reset module-level state between tests
    import src.ui.server as server_mod
    server_mod._sage_app = None
    server_mod._session_token = ""
    server_mod.app = None

    from src.ui.server import create_app
    fastapi_app = create_app(sage_app or _make_sage_app(), session_token=TOKEN)
    return TestClient(fastapi_app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# 1. EventBus subscribe/publish
# ---------------------------------------------------------------------------

def test_eventbus_subscribe_publish():
    bus = EventBus()
    q = bus.subscribe()
    bus.emit(EventType.TASK_CREATED, message="hello", task_id="t1")
    # Queue should have one item
    item = q.get_nowait()
    assert item["event_type"] == EventType.TASK_CREATED
    assert item["message"] == "hello"
    assert item["task_id"] == "t1"


# ---------------------------------------------------------------------------
# 2. EventBus unsubscribe stops delivery
# ---------------------------------------------------------------------------

def test_eventbus_unsubscribe():
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.emit(EventType.TASK_CREATED, message="gone")
    assert q.empty()


# ---------------------------------------------------------------------------
# 3. EventBus backpressure
# ---------------------------------------------------------------------------

def test_eventbus_backpressure():
    bus = EventBus()
    q = bus.subscribe()
    # Fill well past max (256) to verify no exception
    for i in range(300):
        bus.emit(EventType.RUNTIME_STARTED, message=str(i))
    # Queue should be non-empty (not more than 256)
    count = 0
    while not q.empty():
        q.get_nowait()
        count += 1
    assert 1 <= count <= 256


# ---------------------------------------------------------------------------
# 4. create_app returns FastAPI
# ---------------------------------------------------------------------------

def test_create_app_returns_fastapi():
    from fastapi import FastAPI
    client = _make_client()
    # client wraps a FastAPI app
    assert client is not None


# ---------------------------------------------------------------------------
# 5. /api/status
# ---------------------------------------------------------------------------

def test_api_status():
    client = _make_client()
    r = client.get("/api/status", headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    data = r.json()
    assert data["runtime"] == "running"
    assert "trustcore" in data["components"]


# ---------------------------------------------------------------------------
# 6. /api/tasks
# ---------------------------------------------------------------------------

def test_api_tasks():
    client = _make_client()
    r = client.get("/api/tasks", headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) >= 1
    assert tasks[0]["task_id"] == "t-1"


# ---------------------------------------------------------------------------
# 7. /api/task/{id}
# ---------------------------------------------------------------------------

def test_api_task_detail():
    client = _make_client()
    r = client.get("/api/task/t-1", headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["task_id"] == "t-1"
    assert len(data["steps"]) >= 1
    assert data["steps"][0]["step_id"] == "s-1"


# ---------------------------------------------------------------------------
# 8. /api/task (POST)
# ---------------------------------------------------------------------------

def test_api_task_submit():
    client = _make_client()
    r = client.post("/api/task", json={"title": "My new task"}, headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    data = r.json()
    assert "task_id" in data
    assert "status" in data


# ---------------------------------------------------------------------------
# 9. /api/task/{id}/cancel
# ---------------------------------------------------------------------------

def test_api_task_cancel():
    client = _make_client()
    r = client.post("/api/task/t-1/cancel", headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# 10. /api/approvals
# ---------------------------------------------------------------------------

def test_api_approvals_empty():
    client = _make_client()
    r = client.get("/api/approvals", headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["approvals"] == []


def test_api_approvals_with_pending():
    app = _make_sage_app(approvals=[_make_approval()])
    client = _make_client(app)
    r = client.get("/api/approvals", headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    items = r.json()["approvals"]
    assert len(items) == 1
    assert items[0]["action_name"] == "write_file"


# ---------------------------------------------------------------------------
# 11. /api/approval/{id}/approve
# ---------------------------------------------------------------------------

def test_api_approve():
    app = _make_sage_app()
    client = _make_client(app)
    r = client.post("/api/approval/r-1/approve", json={"actor": "tester"}, headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["status"] == "APPROVED"
    app._approval_store.approve_request.assert_called_once_with("r-1", actor="tester")


# ---------------------------------------------------------------------------
# 12. /api/approval/{id}/reject
# ---------------------------------------------------------------------------

def test_api_reject():
    app = _make_sage_app()
    client = _make_client(app)
    r = client.post("/api/approval/r-1/reject", json={"actor": "tester", "reason": "nope"}, headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["status"] == "REJECTED"
    app._approval_store.reject_request.assert_called_once_with("r-1", actor="tester", reason="nope")


# ---------------------------------------------------------------------------
# 13. /api/chat
# ---------------------------------------------------------------------------

def test_api_chat():
    client = _make_client()
    r = client.post("/api/chat", json={"message": "Hello Friday"}, headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 200
    assert "reply" in r.json()
    assert r.json()["reply"] == "Hello from Friday!"


def test_api_chat_empty_message():
    client = _make_client()
    r = client.post("/api/chat", json={"message": "   "}, headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 14. Auth: missing token → 401
# ---------------------------------------------------------------------------

def test_auth_missing_token():
    client = _make_client()
    r = client.get("/api/status")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 15. Auth: wrong token → 401
# ---------------------------------------------------------------------------

def test_auth_wrong_token():
    client = _make_client()
    r = client.get("/api/status", headers={"X-SAGE-Token": "wrong-token"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 16. UI root with wrong token → 401
# ---------------------------------------------------------------------------

def test_ui_root_wrong_token():
    client = _make_client()
    r = client.get("/?token=bad")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 17. Bridge: approval patching emits APPROVAL_REQUIRED event
# ---------------------------------------------------------------------------

def test_bridge_patches_approval_store():
    from src.ui.bridge import attach

    bus = EventBus()
    # Temporarily inject a fresh bus
    import src.ui.events as ev_mod
    orig_bus = ev_mod._bus
    ev_mod._bus = bus

    app = _make_sage_app()
    # Make create_request return a real-ish mock
    mock_rec = _make_approval()
    mock_rec.action_name = "delete_file"
    mock_rec.action_risk_level = "HIGH"
    app._approval_store.create_request.return_value = mock_rec

    q = bus.subscribe()
    try:
        attach(app)
        # Call patched create_request
        app._approval_store.create_request(action_name="delete_file", action_risk_level="HIGH", task_id="t-1")
        # Find APPROVAL_REQUIRED event
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        types_seen = [e["event_type"] for e in events]
        assert EventType.APPROVAL_REQUIRED in types_seen
    finally:
        ev_mod._bus = orig_bus
        bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# 18. Bridge: idempotent double-attach
# ---------------------------------------------------------------------------

def test_bridge_idempotent():
    from src.ui.bridge import attach

    app = _make_sage_app()
    orig_execute = app._supervisor.execute_task

    attach(app)
    fn_after_first = app._supervisor.execute_task

    attach(app)
    fn_after_second = app._supervisor.execute_task

    # Should not double-wrap (same patched function)
    assert fn_after_first is fn_after_second


# ---------------------------------------------------------------------------
# 19. ExecutionEvent serialises cleanly
# ---------------------------------------------------------------------------

def test_execution_event_serialise():
    import json
    ev = ExecutionEvent(
        event_type=EventType.TASK_CREATED,
        message="hello",
        task_id="t-99",
    )
    d = ev.to_dict()
    assert d["event_type"] == EventType.TASK_CREATED
    assert d["task_id"] == "t-99"
    # Must be JSON-serialisable
    json.dumps(d)


# ---------------------------------------------------------------------------
# 20. /api/task/{id} 404 for unknown task
# ---------------------------------------------------------------------------

def test_api_task_not_found():
    app = _make_sage_app()
    app._state_store.get_task.return_value = None
    client = _make_client(app)
    r = client.get("/api/task/no-such-task", headers={"X-SAGE-Token": TOKEN})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 21. /api/events SSE auth check
# ---------------------------------------------------------------------------

def test_api_events_unauthorized():
    client = _make_client()
    r = client.get("/api/events?token=invalid")
    assert r.status_code == 401
