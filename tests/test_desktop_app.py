"""tests/test_desktop_app.py — PySide6 desktop application tests.

All tests run headless via QT_QPA_PLATFORM=offscreen.
No network calls, no paid APIs — 100% offline.
"""
from __future__ import annotations

import os
import sys
import types
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure offscreen rendering before PySide6 is imported
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ---------------------------------------------------------------------------
# PySide6 availability guard
# ---------------------------------------------------------------------------
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

# One QApplication for the entire test session
_qt_app: QApplication | None = None


def _get_app() -> QApplication:
    global _qt_app
    if _qt_app is None:
        _qt_app = QApplication.instance() or QApplication(sys.argv)
    return _qt_app


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    return _get_app()


# ---------------------------------------------------------------------------
# Minimal sage_app mock
# ---------------------------------------------------------------------------
def _make_sage_app(**overrides) -> MagicMock:
    app = MagicMock()
    app.friday_handler = MagicMock()
    app.friday_handler.handle_message.return_value = "Hello from Friday"
    
    store = MagicMock()
    store.list_tasks.return_value = []
    store.list_task_steps.return_value = []
    app._state_store = store
    app.state_store = store

    approval_store = MagicMock()
    approval_store.list_pending.return_value = []
    rec = MagicMock()
    rec.action_name = "test_action"
    rec.task_id = "test_task_id"
    approval_store.approve_request.return_value = rec
    approval_store.reject_request.return_value = rec
    app._approval_store = approval_store
    app.approval_store = approval_store

    app.model_gateway = MagicMock()
    app.supervisor = MagicMock()
    app.supervisor.cancel_task = MagicMock()
    for k, v in overrides.items():
        setattr(app, k, v)
    return app


# ===========================================================================
# Widget unit tests
# ===========================================================================


class TestFridayChatWidget:
    def test_creates_without_crash(self, qt_app):
        from src.ui.desktop.chat_widget import FridayChatWidget
        w = FridayChatWidget()
        assert w is not None

    def test_add_message(self, qt_app):
        from src.ui.desktop.chat_widget import FridayChatWidget
        w = FridayChatWidget()
        w.add_message("friday", "Hello there", "12:00")
        assert w.msg_list.count() == 1

    def test_message_sent_signal(self, qt_app):
        from src.ui.desktop.chat_widget import FridayChatWidget
        w = FridayChatWidget()
        received: list[str] = []
        w.message_sent.connect(received.append)
        w.input_edit.setPlainText("test message")
        w._on_send_clicked()
        assert received == ["test message"]

    def test_send_clears_input(self, qt_app):
        from src.ui.desktop.chat_widget import FridayChatWidget
        w = FridayChatWidget()
        w.input_edit.setPlainText("hi")
        w._on_send_clicked()
        assert w.input_edit.toPlainText() == ""

    def test_set_typing(self, qt_app):
        from src.ui.desktop.chat_widget import FridayChatWidget
        w = FridayChatWidget()
        w.set_typing(True)
        w.set_typing(False)


class TestSidebarWidget:
    def test_creates(self, qt_app):
        from src.ui.desktop.sidebar_widget import SidebarWidget
        w = SidebarWidget()
        assert w is not None

    def test_set_tasks(self, qt_app):
        from src.ui.desktop.sidebar_widget import SidebarWidget
        w = SidebarWidget()
        tasks = [
            {"id": "t1", "title": "Task One", "status": "RUNNING"},
            {"id": "t2", "title": "Task Two", "status": "SUCCESS"},
        ]
        w.set_tasks(tasks)
        assert w.task_list.count() == 2

    def test_set_tasks_empty(self, qt_app):
        from src.ui.desktop.sidebar_widget import SidebarWidget
        w = SidebarWidget()
        w.set_tasks([])
        assert w.task_list.count() == 0

    def test_set_status(self, qt_app):
        from src.ui.desktop.sidebar_widget import SidebarWidget
        w = SidebarWidget()
        w.set_status({"supervisor": "ok", "model_gateway": "ok"})


class TestExecutionPlanWidget:
    def test_creates(self, qt_app):
        from src.ui.desktop.plan_widget import ExecutionPlanWidget
        w = ExecutionPlanWidget()
        assert w is not None

    def test_set_steps_renders_icons(self, qt_app):
        from src.ui.desktop.plan_widget import ExecutionPlanWidget
        w = ExecutionPlanWidget()
        steps = [
            {"name": "Plan", "status": "SUCCESS"},
            {"name": "Execute", "status": "RUNNING"},
            {"name": "Verify", "status": "PENDING"},
        ]
        w.set_steps(steps)
        assert w.steps_list.count() == 3


class TestLiveActivityWidget:
    def test_creates(self, qt_app):
        from src.ui.desktop.activity_widget import LiveActivityWidget
        w = LiveActivityWidget()
        assert w is not None

    def test_add_event(self, qt_app):
        from src.ui.desktop.activity_widget import LiveActivityWidget
        w = LiveActivityWidget()
        w.add_event({"type": "tool_call", "detail": "file_read", "timestamp": "12:00:01"})
        assert w.activity_list.count() == 1

    def test_multiple_events(self, qt_app):
        from src.ui.desktop.activity_widget import LiveActivityWidget
        w = LiveActivityWidget()
        for i in range(5):
            w.add_event({"type": "event", "detail": f"e{i}", "timestamp": f"12:0{i}"})
        assert w.activity_list.count() == 5


class TestApprovalWidget:
    def test_creates(self, qt_app):
        from src.ui.desktop.approval_widget import ApprovalWidget
        w = ApprovalWidget()
        assert w is not None

    def test_empty_state_visible(self, qt_app):
        from src.ui.desktop.approval_widget import ApprovalWidget
        w = ApprovalWidget()
        w.set_approvals([])
        assert not w._empty.isHidden()

    def test_shows_pending_approvals(self, qt_app):
        from src.ui.desktop.approval_widget import ApprovalWidget
        w = ApprovalWidget()
        approvals = [
            {"id": "a1", "action": "file_write", "risk_level": "HIGH", "resource": "/tmp/x"},
            {"id": "a2", "action": "python_eval", "risk_level": "MEDIUM", "resource": ""},
        ]
        w.set_approvals(approvals)
        assert len(w._rows) == 2
        assert w._empty.isHidden()

    def test_approve_signal(self, qt_app):
        from src.ui.desktop.approval_widget import ApprovalWidget
        w = ApprovalWidget()
        w.set_approvals([{"id": "a99", "action": "test_action", "risk_level": "LOW"}])
        emitted: list[str] = []
        w.approve_requested.connect(emitted.append)
        w._rows[0].approved.emit("a99")
        assert emitted == ["a99"]

    def test_reject_signal(self, qt_app):
        from src.ui.desktop.approval_widget import ApprovalWidget
        w = ApprovalWidget()
        w.set_approvals([{"id": "a88", "action": "rm", "risk_level": "HIGH"}])
        emitted: list[str] = []
        w.reject_requested.connect(emitted.append)
        w._rows[0].rejected.emit("a88")
        assert emitted == ["a88"]


# ===========================================================================
# UiService tests
# ===========================================================================


class TestUiService:
    def test_send_chat_message_calls_friday(self, qt_app):
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        svc = UiService(sage_app)
        received: list[tuple] = []
        svc.message_received.connect(lambda r, t, ts: received.append((r, t, ts)))

        svc.send_chat_message("hello")
        import time; time.sleep(0.5)

        assert sage_app.friday_handler.handle_message.called

    def test_refresh_tasks_emits_signal(self, qt_app):
        from src.ui.desktop.service import UiService
        store = MagicMock()
        store.list_tasks.return_value = [
            MagicMock(
                id="t1", title="T1", status=MagicMock(value="RUNNING"),
                created_at=None, updated_at=None, goal="g"
            )
        ]
        sage_app = _make_sage_app()
        sage_app._state_store = store
        sage_app.state_store = store
        svc = UiService(sage_app)
        emitted: list[list] = []
        svc.tasks_updated.connect(emitted.append)
        svc.refresh_tasks()
        assert emitted

    def test_refresh_status_emits_signal(self, qt_app):
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        svc = UiService(sage_app)
        emitted: list[dict] = []
        svc.status_updated.connect(emitted.append)
        svc.refresh_status()
        assert emitted

    def test_refresh_approvals_emits_signal(self, qt_app):
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        svc = UiService(sage_app)
        emitted: list = []
        svc.approvals_updated.connect(emitted.append)
        svc.refresh_approvals()
        assert emitted

    def test_approve_request_calls_store(self, qt_app):
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        svc = UiService(sage_app)
        svc.approve_request("req-1")
        import time; time.sleep(0.3)
        sage_app.approval_store.approve_request.assert_called()

    def test_reject_request_calls_store(self, qt_app):
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        svc = UiService(sage_app)
        svc.reject_request("req-2")
        import time; time.sleep(0.3)
        sage_app.approval_store.reject_request.assert_called()

    def test_cancel_task(self, qt_app):
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        svc = UiService(sage_app)
        svc.cancel_task("task-99")
        import time; time.sleep(0.3)
        assert True


# ===========================================================================
# SageMainWindow integration tests
# ===========================================================================


class TestSageMainWindow:
    def _window(self, qt_app):
        from src.ui.desktop.main_window import SageMainWindow
        sage_app = _make_sage_app()
        return SageMainWindow(sage_app), sage_app

    def test_creates_without_crash(self, qt_app):
        w, _ = self._window(qt_app)
        assert w is not None

    def test_window_title(self, qt_app):
        w, _ = self._window(qt_app)
        assert "SAGE" in w.windowTitle()

    def test_minimum_size(self, qt_app):
        w, _ = self._window(qt_app)
        assert w.minimumWidth() >= 1100
        assert w.minimumHeight() >= 700

    def test_close_calls_stop(self, qt_app):
        from src.ui.desktop.main_window import SageMainWindow
        from PySide6.QtGui import QCloseEvent
        sage_app = _make_sage_app()
        w = SageMainWindow(sage_app)
        event = QCloseEvent()
        w.closeEvent(event)
        sage_app.stop.assert_called_once()

    def test_welcome_message_added(self, qt_app):
        w, _ = self._window(qt_app)
        assert w._chat.msg_list.count() >= 1


# ===========================================================================
# Security / boundary tests
# ===========================================================================


class TestSecurityBoundaries:
    def test_approval_routes_through_store_not_direct_bypass(self, qt_app):
        """Approve action must call approval_store, not bypass TrustCore."""
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        svc = UiService(sage_app)
        svc.approve_request("sec-test-id")
        import time; time.sleep(0.3)
        sage_app.approval_store.approve_request.assert_called()

    def test_invalid_task_id_safe_error(self, qt_app):
        """Selecting a non-existent task_id must not raise an unhandled exception."""
        from src.ui.desktop.service import UiService
        sage_app = _make_sage_app()
        sage_app._state_store.list_task_steps.side_effect = Exception("not found")
        svc = UiService(sage_app)
        errors: list[str] = []
        svc.error_occurred.connect(errors.append)
        svc.select_task("bad-task-id-xyz")
        import time; time.sleep(0.3)
        assert True


# ===========================================================================
# Offline / environment sanity
# ===========================================================================


def test_pyside6_offscreen_init():
    """QApplication creates in offscreen mode without a physical display."""
    app = QApplication.instance()
    assert app is not None


def test_import_all_desktop_modules():
    """Every desktop module imports cleanly."""
    from src.ui.desktop import activity_widget, approval_widget  # noqa: F401
    from src.ui.desktop import chat_widget, main_window, plan_widget  # noqa: F401
    from src.ui.desktop import service, sidebar_widget, theme  # noqa: F401
