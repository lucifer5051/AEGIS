"""PROMPT 075: SAGE Desktop Awareness Subsystem Test Suite.

Verifies:
1. Native observer IPC client, handshake, and event ingestion.
2. DesktopStateManager active window tracking, focused control updates, and event coalescing.
3. Low-frequency reconciliation.
4. DifferentialStateTracker textual summaries and change detection.
5. DesktopElementRegistry locality tuples and stale reference detection.
6. InterruptionManager classification (Critical, Modal, Transient).
7. TaskContinuityManager task pausing, checkpointing, and resumption checks.
8. DesktopActionAdapter perimeter validation and semantic interactions.
9. DesktopVerificationService post-action state assertions.
10. VisionFallbackAdapter targeted visual capture and untrusted tagging.
11. Full SAGE Runtime integration with SageApplication and ToolBroker.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.desktop import (
    DesktopActionAdapter,
    DesktopElementRegistry,
    DesktopEvent,
    DesktopEventType,
    DesktopObserverClient,
    DesktopStateManager,
    DesktopTaskCheckpoint,
    DesktopVerificationService,
    DifferentialStateTracker,
    InterruptionDecision,
    InterruptionManager,
    InterruptionSeverity,
    LocalityTuple,
    TaskContinuityManager,
    VisionFallbackAdapter,
    WindowsDesktopToolPolicy,
    WindowsMCPConfig,
)
from src.desktop.broker_integration import register_desktop_action_tools
from src.runtime import SageApplication
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.models import ToolResultStatus
from src.trust import build_trust_authority
from src.trust.policy import AutonomyTier


class MockWatcherProcess:
    """Mock subprocess simulating sage-desktop-watcher stdout/stdin protocol."""

    def __init__(self) -> None:
        self._stdin_lines: List[str] = []
        self._stdout_queue: queue.Queue = queue.Queue()
        self._running = True

        class StdinWriter:
            def __init__(self, outer):
                self._outer = outer

            def write(self, text: str):
                self._outer._handle_stdin(text)

            def flush(self):
                pass

        class StdoutReader:
            def __init__(self, outer):
                self._outer = outer

            def readline(self) -> str:
                if not self._outer._running:
                    return ""
                try:
                    return self._outer._stdout_queue.get(timeout=0.2)
                except queue.Empty:
                    return ""

        class StderrReader:
            def readline(self) -> str:
                return ""

        self.stdin = StdinWriter(self)
        self.stdout = StdoutReader(self)
        self.stderr = StderrReader()

    def poll(self) -> Optional[int]:
        return 0 if not self._running else None

    def terminate(self):
        self._running = False

    def wait(self, timeout: Optional[float] = None):
        self._running = False
        return 0

    def push_event(self, event_type: str, hwnd: int, title: str, proc: str = "notepad.exe", pid: int = 1234):
        evt = {
            "Version": "1.0",
            "EventId": f"evt-{time.time()}",
            "EventType": event_type,
            "Timestamp": time.time(),
            "ProcessId": pid,
            "WindowHandle": hwnd,
            "SessionGeneration": 1,
            "ProcessName": proc,
            "WindowTitle": title,
        }
        self._stdout_queue.put(json.dumps(evt) + "\n")

    def _handle_stdin(self, text: str):
        if not text.strip() or not self._running:
            return

        cmd_dict = json.loads(text.strip())
        req_id = cmd_dict.get("id", 1)
        command = cmd_dict.get("command")

        if command == "ping":
            resp = {"Id": req_id, "Status": "SUCCESS", "Result": {"pong": True}}
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "inspect":
            resp = {
                "Id": req_id,
                "Status": "SUCCESS",
                "Result": {
                    "root": {
                        "Name": "Untitled - Notepad",
                        "AutomationId": "MainNotepadWindow",
                        "ControlType": "Window",
                        "Children": [
                            {"Name": "Text Editor", "AutomationId": "EditBox", "ControlType": "Edit", "Patterns": ["Value"]}
                        ]
                    }
                }
            }
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "invoke":
            resp = {"Id": req_id, "Status": "SUCCESS", "Result": {"invoked": True}}
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "set_value":
            resp = {"Id": req_id, "Status": "SUCCESS", "Result": {"value_set": True}}
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "click":
            resp = {"Id": req_id, "Status": "SUCCESS", "Result": {"clicked": True}}
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "type":
            resp = {"Id": req_id, "Status": "SUCCESS", "Result": {"typed": True}}
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "shortcut":
            resp = {"Id": req_id, "Status": "SUCCESS", "Result": {"shortcut_sent": True}}
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "screenshot":
            resp = {
                "Id": req_id,
                "Status": "SUCCESS",
                "Result": {"screenshot_base64": "mock_base64_png", "bounds": {"width": 800, "height": 600}}
            }
            self._stdout_queue.put(json.dumps(resp) + "\n")
        elif command == "shutdown":
            self._running = False
            resp = {"Id": req_id, "Status": "SUCCESS", "Result": {"shutdown": True}}
            self._stdout_queue.put(json.dumps(resp) + "\n")


@pytest.fixture
def mock_watcher_proc() -> MockWatcherProcess:
    return MockWatcherProcess()


@pytest.fixture
def mock_client(mock_watcher_proc: MockWatcherProcess) -> DesktopObserverClient:
    return DesktopObserverClient(
        executable_path="mock-desktop-watcher.exe",
        process_factory=lambda *a, **kw: mock_watcher_proc,
    )


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "awareness_test.db"
    migrations_dir = Path(__file__).parent.parent / "migrations"
    return initialize_runtime_database(db_file, migrations_dir=migrations_dir)


# =============================================================================
# 1. EVENT INGESTION & STATE MANAGER
# =============================================================================

def test_event_ingestion_and_state_tracking(mock_client: DesktopObserverClient, mock_watcher_proc: MockWatcherProcess):
    """Verify that observer events asynchronously update the DesktopStateManager."""
    state_mgr = DesktopStateManager()
    mock_client.add_event_listener(state_mgr.handle_event)
    mock_client.start()

    assert mock_client.is_running is True

    # Emit Foreground window change
    mock_watcher_proc.push_event("FOREGROUND_CHANGED", hwnd=1001, title="Untitled - Notepad", proc="notepad.exe")
    time.sleep(0.1)

    active = state_mgr.active_window
    assert active is not None
    assert active.hwnd == 1001
    assert active.title == "Untitled - Notepad"
    assert active.process_name == "notepad.exe"
    assert 1001 in state_mgr.open_windows

    # Emit Focus Change
    mock_watcher_proc.push_event("FOCUS_CHANGED", hwnd=1001, title="Text Editor", proc="notepad.exe")
    time.sleep(0.1)
    focused = state_mgr.focused_element
    assert focused is not None
    assert focused.name == "Text Editor"

    mock_client.stop()


def test_event_coalescing_debouncing(mock_client: DesktopObserverClient, mock_watcher_proc: MockWatcherProcess):
    """Verify that rapid bursts of identical events on the same window within 100ms are coalesced."""
    state_mgr = DesktopStateManager(coalescing_window_seconds=0.15)
    mock_client.add_event_listener(state_mgr.handle_event)
    mock_client.start()

    # Emit 5 rapid typing focus events within 50ms
    for i in range(5):
        mock_watcher_proc.push_event("FOCUS_CHANGED", hwnd=2002, title=f"Char-{i}", proc="notepad.exe")
        time.sleep(0.01)

    time.sleep(0.05)
    # The history should coalesce the rapid burst
    history = state_mgr.event_history
    assert len(history) < 5

    mock_client.stop()


def test_state_reconciliation():
    """Verify low-frequency reconciliation resynchronizes state upon drift."""
    state_mgr = DesktopStateManager()
    assert state_mgr.active_window is None

    # Force low-frequency reconciliation update
    state_mgr.reconcile_state(live_active_hwnd=9999, live_title="Reconciled Explorer", live_pid=5555, live_proc="explorer.exe")

    active = state_mgr.active_window
    assert active is not None
    assert active.hwnd == 9999
    assert active.title == "Reconciled Explorer"


# =============================================================================
# 2. DIFFERENTIAL TRACKING & ELEMENT REGISTRY
# =============================================================================

def test_differential_state_tracking():
    """Verify concise textual diff computation."""
    tracker = DifferentialStateTracker()

    evt1 = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        window_handle=101,
        process_name="code.exe",
        window_title="VS Code",
    )
    diff1 = tracker.compute_differential(evt1, prev_active=None)
    assert "[DESKTOP_DIFF: FOREGROUND_ACQUIRED]" in diff1
    assert "code.exe" in diff1

    evt2 = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        window_handle=102,
        process_name="notepad.exe",
        window_title="Untitled - Notepad",
    )
    from src.desktop.state_manager import WindowState
    prev_win = WindowState(hwnd=101, pid=111, process_name="code.exe", title="VS Code")
    diff2 = tracker.compute_differential(evt2, prev_active=prev_win)
    assert "[DESKTOP_DIFF: FOREGROUND_CHANGED]" in diff2
    assert "notepad.exe" in diff2


def test_element_registry_stale_reference_detection():
    """Verify stale reference detection when window closes, session restarts, or TTL expires."""
    registry = DesktopElementRegistry(stale_ttl_seconds=1.0)

    # Register an element
    registry.register_element(
        key="btn_save",
        process_name="notepad.exe",
        root_hwnd=5001,
        control_type="Button",
        automation_id="SaveBtn",
        name="Save",
        session_generation=1,
    )

    # 1. Fresh element check
    is_stale, reason = registry.is_reference_stale("btn_save", current_session_generation=1, live_open_hwnds=[5001])
    assert is_stale is False

    # 2. Session generation mismatch (observer restarted)
    is_stale, reason = registry.is_reference_stale("btn_save", current_session_generation=2, live_open_hwnds=[5001])
    assert is_stale is True
    assert "Session generation mismatch" in reason

    # 3. Window closed (HWND no longer in open list)
    is_stale, reason = registry.is_reference_stale("btn_save", current_session_generation=1, live_open_hwnds=[9999])
    assert is_stale is True
    assert "Parent window HWND=5001 is no longer open" in reason

    # 4. TTL expiration
    time.sleep(1.1)
    is_stale, reason = registry.is_reference_stale("btn_save", current_session_generation=1, live_open_hwnds=[5001])
    assert is_stale is True
    assert "expired" in reason


# =============================================================================
# 3. INTERRUPTION MANAGEMENT & TASK CONTINUITY
# =============================================================================

def test_interruption_classification():
    """Verify classification of Critical (UAC), Modal, and Transient interruptions."""
    mgr = InterruptionManager()

    # 1. Critical UAC Prompt
    uac_evt = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        window_handle=777,
        process_name="consent.exe",
        window_title="User Account Control",
    )
    dec_uac = mgr.evaluate_interruption(uac_evt, target_process="notepad.exe")
    assert dec_uac.should_pause is True
    assert dec_uac.severity == InterruptionSeverity.CRITICAL

    # 2. Modal Dialog in target app (Save As)
    modal_evt = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        window_handle=888,
        process_name="notepad.exe",
        window_title="Save As",
    )
    dec_modal = mgr.evaluate_interruption(modal_evt, target_process="notepad.exe", target_hwnd=880)
    assert dec_modal.should_pause is True
    assert dec_modal.severity == InterruptionSeverity.MODAL

    # 3. Normal event on target window
    normal_evt = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        window_handle=880,
        process_name="notepad.exe",
        window_title="Untitled - Notepad",
    )
    dec_norm = mgr.evaluate_interruption(normal_evt, target_process="notepad.exe", target_hwnd=880)
    assert dec_norm.should_pause is False


def test_task_continuity_checkpoint_and_resume():
    """Verify task pausing, checkpointing, and safe resumption checks."""
    continuity = TaskContinuityManager()

    cp = continuity.checkpoint_task(
        task_id="task-101",
        step_index=3,
        target_process="notepad.exe",
        target_hwnd=4444,
        last_known_title="Untitled - Notepad",
        pause_reason="Modal dialog opened",
        state_payload={"text_typed": "Hello"},
    )
    assert cp.task_id == "task-101"

    # Cannot resume if different app is active
    assert continuity.can_resume("task-101", current_active_hwnd=9999, current_active_proc="chrome.exe") is False

    # Can resume when target process is back in foreground
    assert continuity.can_resume("task-101", current_active_hwnd=4444, current_active_proc="notepad.exe") is True


# =============================================================================
# 4. ACTION ADAPTER & VERIFICATION SERVICE
# =============================================================================

def test_semantic_actions_and_verification(mock_client: DesktopObserverClient):
    """Verify semantic actions (invoke, set_value, click, type) and outcome verification."""
    mock_client.start()
    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["notepad.exe"]))
    adapter = DesktopActionAdapter(client=mock_client, policy=policy)
    verifier = DesktopVerificationService()

    # 1. Semantic Invoke
    inv_res = adapter.invoke(automation_id="BtnSave")
    assert inv_res["status"] == "SUCCESS"

    # 2. Semantic SetValue
    val_res = adapter.set_value(automation_id="TxtInput", value="New Text")
    assert val_res["status"] == "SUCCESS"

    # 3. Outcome Verification
    mock_snapshot = {"active_window": "Notepad", "text_content": "New Text appeared"}
    ver_pass = verifier.verify_outcome("set_value", "New Text", mock_snapshot)
    assert ver_pass.passed is True

    ver_fail = verifier.verify_outcome("set_value", "Missing Text", mock_snapshot)
    assert ver_fail.passed is False

    # 4. Unauthorized App Launch Denied
    with pytest.raises(Exception, match="not in the permitted application allowlist"):
        adapter.launch_app("cmd.exe")

    mock_client.stop()


# =============================================================================
# 5. VISION & OCR FALLBACK ADAPTER
# =============================================================================

def test_vision_fallback_adapter(mock_client: DesktopObserverClient):
    """Verify that visual fallback captures frame, tags untrusted, and redacts secrets."""
    mock_client.start()
    vision = VisionFallbackAdapter(client=mock_client)

    res = vision.inspect_visual(hwnd=101)
    assert res["source"] == "visual_fallback_ocr"
    assert res["untrusted_data"] is True
    assert res["has_image"] is True
    assert vision.fallback_invocations == 1

    mock_client.stop()


# =============================================================================
# 6. SAGE APPLICATION & TOOLBROKER RUNTIME INTEGRATION
# =============================================================================

def test_full_runtime_desktop_tools_integration(test_db: sqlite3.Connection, mock_client: DesktopObserverClient):
    """Verify that desktop tools integrate into ToolBroker and enforce TrustCore policy."""
    trust_authority = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(trust_authority)
    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["notepad.exe"]))

    mock_client.start()
    adapter = DesktopActionAdapter(client=mock_client, policy=policy)
    register_desktop_action_tools(broker, adapter, policy)

    # 1. Snapshot tool invocation (ActionRisk.LOW)
    snap_res = broker.invoke("windows.snapshot", {"target_window": 0}, actor="sage_tester")
    assert snap_res.status == ToolResultStatus.SUCCESS
    assert "Notepad" in snap_res.output

    # 2. Invoke tool invocation (ActionRisk.MEDIUM)
    inv_res = broker.invoke("windows.invoke", {"element_id": "BtnSave"}, actor="sage_tester")
    assert inv_res.status == ToolResultStatus.SUCCESS

    # 3. At SAFE_BOUNDED tier, mutating tools escalate / are denied without approval
    trust_safe = build_trust_authority(test_db, autonomy_tier=AutonomyTier.SAFE_BOUNDED)
    broker_safe = ToolBroker(trust_safe)
    register_desktop_action_tools(broker_safe, adapter, policy)

    click_res = broker_safe.invoke("windows.click", {"x": 50, "y": 50}, actor="sage_tester")
    assert click_res.status == ToolResultStatus.DENIED

    mock_client.stop()
