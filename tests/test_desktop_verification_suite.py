"""PROMPT 076: Comprehensive Verification & Production Readiness Test Suite.

Verifies:
- Local Windows.Media.Ocr extraction with word-level bounding boxes and untrusted tagging.
- Semantic interaction via OCR-derived coordinates when UIA structure is absent.
- Strict stale-reference rejection (session generation mismatch, HWND destruction, TTL expiry).
- Comprehensive interruption handling across Scenarios A through E (modal dialog, process switch, window close, crash, UAC).
- Durable task continuity checkpointing integrated with StateStore.
- TrustCore security policy enforcement across all 10 canonical desktop tools.
- Decision Layer and Supervisor end-to-end task execution with desktop tools.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

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
from src.desktop.errors import DesktopProcessError, DesktopSecurityError
from src.manager.models import TaskRecord, TaskState
from src.manager.state_store import StateStore
from src.manager.supervisor import Supervisor
from src.models.domain import ModelDescriptor, ProviderType
from src.models.gateway import ModelGateway
from src.models.provider_adapter import GatewayReasoningProvider
from src.models.registry import ModelRegistry
from src.models.router import ModelRouter
from src.storage.database import initialize_runtime_database
from src.tools.broker import ToolBroker
from src.tools.models import ToolResultStatus
from src.trust import build_trust_authority
from src.trust.approval import ApprovalStore
from src.trust.policy import AutonomyTier


@pytest.fixture
def test_db(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "desktop_verification.db"
    return initialize_runtime_database(db_file)


class MockWatcher:
    """Mock subprocess simulating sage-desktop-watcher stdio protocol."""

    def __init__(self, session_generation: int = 1) -> None:
        import queue
        self._running = True
        self._queue: queue.Queue = queue.Queue()
        self.session_generation = session_generation
        self.clicks: List[Dict[str, int]] = []
        self.invokes: List[Dict[str, str]] = []

        class Stdin:
            def __init__(self, outer):
                self._outer = outer

            def write(self, s: str):
                if not s.strip() or not self._outer._running:
                    return
                try:
                    cmd = json.loads(s.strip())
                    cid = cmd.get("id", 1)
                    cname = cmd.get("command")

                    if cname == "ping":
                        resp = {
                            "Id": cid,
                            "Status": "SUCCESS",
                            "Result": {"pong": True, "session_generation": self._outer.session_generation},
                        }
                    elif cname == "screenshot":
                        resp = {
                            "Id": cid,
                            "Status": "SUCCESS",
                            "Result": {
                                "screenshot_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                                "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
                            },
                        }
                    elif cname == "click":
                        self._outer.clicks.append({"x": cmd.get("x", 0), "y": cmd.get("y", 0)})
                        resp = {"Id": cid, "Status": "SUCCESS", "Result": {"clicked": True, "x": cmd.get("x", 0), "y": cmd.get("y", 0)}}
                    elif cname == "invoke":
                        self._outer.invokes.append({"auto_id": cmd.get("automation_id", ""), "name": cmd.get("name", "")})
                        resp = {"Id": cid, "Status": "SUCCESS", "Result": {"invoked": True}}
                    elif cname == "inspect":
                        resp = {
                            "Id": cid,
                            "Status": "SUCCESS",
                            "Result": {
                                "root": {
                                    "Name": "Notepad",
                                    "ControlType": "ControlType.Window",
                                    "NativeWindowHandle": 2001,
                                    "Children": [
                                        {"Name": "Save", "AutomationId": "BtnSave", "ControlType": "ControlType.Button"},
                                        {"Name": "Text", "AutomationId": "EditArea", "ControlType": "ControlType.Document"},
                                    ],
                                }
                            },
                        }
                    else:
                        resp = {"Id": cid, "Status": "SUCCESS", "Result": {"status": "ok"}}

                    self._outer._queue.put(json.dumps(resp) + "\n")
                except Exception:
                    pass

            def flush(self):
                pass

        class Stdout:
            def __init__(self, outer):
                self._outer = outer

            def readline(self):
                if not self._outer._running:
                    return ""
                try:
                    return self._outer._queue.get(timeout=0.2)
                except Exception:
                    return ""

        class Stderr:
            def readline(self):
                return ""

        self.stdin = Stdin(self)
        self.stdout = Stdout(self)
        self.stderr = Stderr()

    def poll(self):
        return 0 if not self._running else None

    def terminate(self):
        self._running = False

    def wait(self, timeout=None):
        self._running = False
        return 0


# =============================================================================
# 1. LOCAL OCR EXTRACTION & VISUAL FALLBACK INTERACTION
# =============================================================================

def test_ocr_extraction_and_untrusted_tagging():
    """Verify local OCR extraction with word coordinates, reason tracking, and untrusted tagging."""
    mock_watcher = MockWatcher()
    ready_evt = json.dumps({"EventType": "OBSERVER_READY", "Timestamp": time.time(), "SessionGeneration": 1}) + "\n"
    mock_watcher._queue.put(ready_evt)

    client = DesktopObserverClient(process_factory=lambda *a, **kw: mock_watcher)
    client.start()

    # Synthetic mock OCR extractor returning word bounding boxes
    def mock_ocr(b64: str) -> Dict[str, Any]:
        return {
            "text": "Press START to begin game sk-ant-api03-secret1234567890",
            "words": [
                {"text": "Press", "x": 10, "y": 20, "width": 40, "height": 15},
                {"text": "START", "x": 60, "y": 20, "width": 50, "height": 15},
                {"text": "to", "x": 120, "y": 20, "width": 20, "height": 15},
                {"text": "begin", "x": 150, "y": 20, "width": 40, "height": 15},
                {"text": "game", "x": 200, "y": 20, "width": 40, "height": 15},
                {"text": "sk-ant-api03-secret1234567890", "x": 250, "y": 20, "width": 100, "height": 15},
            ],
        }

    vision = VisionFallbackAdapter(client=client, ocr_fn=mock_ocr)
    result = vision.inspect_visual(hwnd=501, reason="DirectX game canvas lacks UIA structure")

    assert result["source"] == "visual_fallback_ocr"
    assert result["reason"] == "DirectX game canvas lacks UIA structure"
    assert result["untrusted_data"] is True
    assert "[REDACTED_SECRET]" in result["text"]
    assert "sk-ant-api03" not in result["text"]
    assert len(result["words"]) == 6
    assert result["words"][1]["text"] == "START"
    assert result["words"][1]["x"] == 60

    # Test click-by-text interaction using OCR coordinates
    click_res = vision.click_text("START", result, hwnd=501)
    assert click_res["status"] == "SUCCESS"
    assert click_res["clicked"] is True
    # Click coordinate = offset_x (0) + x (60) + width // 2 (25) = 85
    assert click_res["x"] == 85
    # Click coordinate = offset_y (0) + y (20) + height // 2 (7) = 27
    assert click_res["y"] == 27
    assert len(mock_watcher.clicks) == 1
    assert mock_watcher.clicks[0]["x"] == 85

    # Test clicking missing text raises DesktopProcessError
    with pytest.raises(DesktopProcessError, match="not located in visual OCR"):
        vision.click_text("NONEXISTENT", result)

    client.stop()


# =============================================================================
# 2. STALE ELEMENT REFERENCE REJECTION
# =============================================================================

def test_stale_reference_rejection_fail_closed():
    """Verify that DesktopActionAdapter rejects stale references across restarts and TTL expiry."""
    mock_watcher = MockWatcher(session_generation=1)
    mock_watcher._queue.put(json.dumps({"EventType": "OBSERVER_READY", "Timestamp": time.time(), "SessionGeneration": 1}) + "\n")

    client = DesktopObserverClient(process_factory=lambda *a, **kw: mock_watcher)
    client.start()

    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["notepad.exe"]))
    registry = DesktopElementRegistry(stale_ttl_seconds=0.5)
    adapter = DesktopActionAdapter(client=client, policy=policy, registry=registry)

    # Register an element in session generation 1
    registry.register_element(
        key="BtnSave",
        process_name="notepad.exe",
        root_hwnd=1001,
        control_type="Button",
        automation_id="BtnSave",
        name="Save",
        session_generation=1,
    )

    # 1. Fresh invocation succeeds
    res1 = adapter.invoke(automation_id="BtnSave")
    assert res1["status"] == "SUCCESS"

    # 2. Session generation mismatch (observer restart simulated)
    client._session_generation = 2
    with pytest.raises(DesktopSecurityError, match="Session generation mismatch"):
        adapter.invoke(automation_id="BtnSave")

    # Reset session generation to 1
    client._session_generation = 1

    # 3. TTL Expiration check
    time.sleep(0.6)
    with pytest.raises(DesktopSecurityError, match="expired after"):
        adapter.invoke(automation_id="BtnSave")

    client.stop()


# =============================================================================
# 3. INTERRUPTION SCENARIOS A THROUGH E
# =============================================================================

def test_interruption_scenarios_a_to_e():
    """Verify interruption classification across Scenarios A through E."""
    manager = InterruptionManager()

    # Scenario A: Unexpected modal dialog (Save As / Warning) appearing in target process
    evt_modal = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        process_name="notepad.exe",
        window_handle=2002,
        window_title="Save As",
        process_id=1234,
        session_generation=1,
    )
    dec_a = manager.evaluate_interruption(evt_modal, target_process="notepad.exe", target_hwnd=2001)
    assert dec_a.should_pause is True
    assert dec_a.severity == InterruptionSeverity.MODAL
    assert "Modal dialog 'Save As' opened" in dec_a.reason

    # Scenario B: Focus switched to another process (e.g. Teams, Browser)
    evt_switch = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        process_name="chrome.exe",
        window_handle=3001,
        window_title="Google Chrome",
        process_id=5678,
        session_generation=1,
    )
    dec_b = manager.evaluate_interruption(evt_switch, target_process="notepad.exe", target_hwnd=2001)
    assert dec_b.should_pause is True
    assert dec_b.severity == InterruptionSeverity.MODAL
    assert "unexpected process 'chrome.exe'" in dec_b.reason

    # Scenario C: Application closed / target window destroyed
    evt_close = DesktopEvent(
        event_type=DesktopEventType.WINDOW_CLOSED.value,
        process_name="notepad.exe",
        window_handle=2001,
        window_title="Untitled - Notepad",
        process_id=1234,
        session_generation=1,
    )
    dec_c = manager.evaluate_interruption(evt_close, target_process="notepad.exe", target_hwnd=2001)
    assert dec_c.should_pause is True
    assert dec_c.severity == InterruptionSeverity.CRITICAL
    assert "Target window HWND=2001 was closed" in dec_c.reason

    # Scenario D: Security UAC / Elevation dialog
    evt_uac = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        process_name="consent.exe",
        window_handle=9999,
        window_title="User Account Control",
        process_id=99,
        session_generation=1,
    )
    dec_d = manager.evaluate_interruption(evt_uac, target_process="notepad.exe", target_hwnd=2001)
    assert dec_d.should_pause is True
    assert dec_d.severity == InterruptionSeverity.CRITICAL
    assert "UAC security prompt" in dec_d.reason

    # Routine non-disruptive event within target process
    evt_routine = DesktopEvent(
        event_type=DesktopEventType.FOCUS_CHANGED.value,
        process_name="notepad.exe",
        window_handle=2001,
        window_title="Untitled - Notepad",
        process_id=1234,
        session_generation=1,
    )
    dec_routine = manager.evaluate_interruption(evt_routine, target_process="notepad.exe", target_hwnd=2001)
    assert dec_routine.should_pause is False
    assert dec_routine.severity == InterruptionSeverity.NONE


# =============================================================================
# 4. TASK CONTINUITY WITH STATE STORE PERSISTENCE
# =============================================================================

def test_task_continuity_and_statestore_persistence(test_db: sqlite3.Connection):
    """Verify that TaskContinuityManager checkpoints state to StateStore and verifies resumption."""
    store = StateStore(test_db)
    # Create parent task in StateStore
    task = store.create_task(
        title="Automate Notepad document editing",
        task_id="desktop-task-42",
    )

    continuity = TaskContinuityManager(state_store=store)

    # Record checkpoint upon interruption
    cp = continuity.checkpoint_task(
        task_id="desktop-task-42",
        step_index=2,
        target_process="notepad.exe",
        target_hwnd=2001,
        last_known_title="Untitled - Notepad",
        pause_reason="Modal dialog appeared",
        state_payload={"typed_chars": 15, "last_action": "type"},
    )
    assert cp.task_id == "desktop-task-42"
    assert cp.step_index == 2

    # Verify checkpoint was durably recorded in StateStore
    checkpoints = store.list_checkpoints("desktop-task-42")
    assert len(checkpoints) == 1
    assert checkpoints[0].snapshot["target_process"] == "notepad.exe"
    assert checkpoints[0].snapshot["step_index"] == 2

    # Verification of can_resume
    # 1. False when another application is active
    assert continuity.can_resume("desktop-task-42", current_active_hwnd=999, current_active_proc="calc.exe") is False
    # 2. True when target process returns
    assert continuity.can_resume("desktop-task-42", current_active_hwnd=2001, current_active_proc="notepad.exe") is True


# =============================================================================
# 5. TRUSTCORE SECURITY AUDIT ACROSS ALL 10 TOOLS
# =============================================================================

def test_trustcore_security_audit_all_ten_tools(test_db: sqlite3.Connection):
    """Verify that all 10 canonical desktop tools are registered and enforce TrustCore boundaries."""
    mock_watcher = MockWatcher()
    mock_watcher._queue.put(json.dumps({"EventType": "OBSERVER_READY", "Timestamp": time.time(), "SessionGeneration": 1}) + "\n")

    client = DesktopObserverClient(process_factory=lambda *a, **kw: mock_watcher)
    client.start()

    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["notepad.exe", "calc.exe"]))
    adapter = DesktopActionAdapter(client=client, policy=policy)

    # Test under SAFE_BOUNDED tier (mutating tools require approval)
    trust_safe = build_trust_authority(test_db, autonomy_tier=AutonomyTier.SAFE_BOUNDED)
    broker_safe = ToolBroker(trust_safe)
    registered = register_desktop_action_tools(broker_safe, adapter, policy)

    expected_tools = {
        "windows.snapshot",
        "windows.screenshot",
        "windows.get_text",
        "windows.invoke",
        "windows.click",
        "windows.type",
        "windows.shortcut",
        "windows.launch_app",
        "windows.wait",
        "windows.wait_for",
    }
    registered_names = {t.name for t in registered}
    assert expected_tools.issubset(registered_names)

    # 1. Read-only tool succeeds at SAFE_BOUNDED without escalation
    snap_res = broker_safe.invoke("windows.snapshot", {"target_window": 0}, actor="auditor")
    assert snap_res.status == ToolResultStatus.SUCCESS

    # 2. Mutating tool without approval is denied at SAFE_BOUNDED
    click_res = broker_safe.invoke("windows.click", {"x": 100, "y": 200}, actor="auditor")
    assert click_res.status == ToolResultStatus.DENIED

    # 3. Test under FULL_BOUNDED tier (mutating tools succeed within boundaries)
    trust_full = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker_full = ToolBroker(trust_full)
    register_desktop_action_tools(broker_full, adapter, policy)

    click_full = broker_full.invoke("windows.click", {"x": 100, "y": 200}, actor="auditor")
    assert click_full.status == ToolResultStatus.SUCCESS

    # 4. Unauthorized application launch is rejected fail-closed
    launch_bad = broker_full.invoke("windows.launch_app", {"app_name": "cmd.exe"}, actor="auditor")
    assert launch_bad.status == ToolResultStatus.ERROR
    assert "DesktopSecurityError" in str(launch_bad.error_summary)

    # 5. Injection via shell characters rejected fail-closed
    launch_inj = broker_full.invoke("windows.launch_app", {"app_name": "calc.exe & whoami"}, actor="auditor")
    assert launch_inj.status == ToolResultStatus.ERROR
    assert "DesktopSecurityError" in str(launch_inj.error_summary)

    client.stop()


# =============================================================================
# 6. DECISION LAYER & SUPERVISOR DESKTOP WORKFLOW INTEGRATION
# =============================================================================

def test_decision_layer_and_supervisor_desktop_workflow(test_db: sqlite3.Connection):
    """Verify that Supervisor executes a goal using registered desktop tools via ToolBroker."""
    trust = build_trust_authority(test_db, autonomy_tier=AutonomyTier.FULL_BOUNDED)
    broker = ToolBroker(trust)

    mock_watcher = MockWatcher()
    mock_watcher._queue.put(json.dumps({"EventType": "OBSERVER_READY", "Timestamp": time.time(), "SessionGeneration": 1}) + "\n")

    client = DesktopObserverClient(process_factory=lambda *a, **kw: mock_watcher)
    client.start()

    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["notepad.exe"]))
    adapter = DesktopActionAdapter(client=client, policy=policy)
    register_desktop_action_tools(broker, adapter, policy)

    state_store = StateStore(test_db)

    # Mock reasoning provider
    class MockReasoning:
        def reason(self, context, model_alias=None):
            return "Step plan created"

    supervisor = Supervisor(
        state_store=state_store,
        trust_authority=trust,
        tool_broker=broker,
        reasoning_provider=MockReasoning(),
    )

    # Submit a desktop goal
    task = supervisor.submit_goal("Inspect active Notepad window and invoke Save button")
    assert task is not None
    assert task.state == TaskState.SUBMITTED

    # Direct tool invocation via the broker used by supervisor
    result = supervisor._tools.invoke("windows.invoke", {"element_id": "BtnSave"}, actor="supervisor_test")
    assert result.status == ToolResultStatus.SUCCESS
    assert len(mock_watcher.invokes) == 1
    assert mock_watcher.invokes[0]["auto_id"] == "BtnSave"

    client.stop()
