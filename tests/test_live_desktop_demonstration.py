"""PROMPT 075: Live Windows Desktop Awareness & Observation Demonstration.

Executes against the compiled native sage-desktop-watcher.exe binary on the live Windows desktop.
Verifies:
1. Live binary execution and handshake.
2. Real-time UIA inspection of live active Windows desktop elements.
3. Live screenshot capture and bounds calculation.
4. Clean shutdown and handle release.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from src.desktop import (
    DesktopActionAdapter,
    DesktopObserverClient,
    DesktopStateManager,
    VisionFallbackAdapter,
    WindowsDesktopToolPolicy,
    WindowsMCPConfig,
)

WATCHER_EXE = (
    Path(__file__).parent.parent
    / "tools"
    / "sage-desktop-watcher"
    / "bin"
    / "Release"
    / "net9.0-windows"
    / "sage-desktop-watcher.exe"
)


@pytest.mark.skipif(not WATCHER_EXE.exists(), reason="sage-desktop-watcher.exe not built")
def test_live_native_watcher_handshake_and_inspection():
    """Execute live observer binary and perform native UI Automation inspection on Windows."""
    client = DesktopObserverClient(executable_path=str(WATCHER_EXE), startup_timeout_seconds=8.0)
    state_mgr = DesktopStateManager()
    client.add_event_listener(state_mgr.handle_event)

    try:
        # 1. Start live observer process
        client.start()
        assert client.is_running is True

        # 2. Handshake ping
        pong = client.ping()
        assert pong.get("pong") is True

        # 3. Live UIA Inspection of current desktop / foreground window
        inspect_res = client.inspect(hwnd=0, max_depth=3)
        assert "root" in inspect_res
        root_node = inspect_res["root"]
        assert "Name" in root_node or "ControlType" in root_node

        # 4. Live Screenshot capture
        screenshot_res = client.screenshot(hwnd=0)
        assert "screenshot_base64" in screenshot_res
        assert len(screenshot_res["screenshot_base64"]) > 100
        bounds = screenshot_res.get("bounds", {})
        assert bounds.get("width", 0) > 0
        assert bounds.get("height", 0) > 0

        # 5. Semantic Action Adapter with Policy allowlist
        policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["notepad.exe"]))
        adapter = DesktopActionAdapter(client=client, policy=policy)
        snap = adapter.snapshot(hwnd=0, max_depth=2)
        assert snap["untrusted_data"] is True
        assert snap["source"] == "windows_desktop"

        # 6. Visual Fallback on live window
        vision = VisionFallbackAdapter(client=client)
        vis_res = vision.inspect_visual(hwnd=0)
        assert vis_res["source"] == "visual_fallback_ocr"
        assert vis_res["has_image"] is True

    finally:
        # 7. Clean graceful shutdown
        client.stop()
        assert client.is_running is False


@pytest.mark.skipif(not WATCHER_EXE.exists(), reason="sage-desktop-watcher.exe not built")
def test_live_desktop_benchmarking_and_awareness():
    """Benchmark live latency of observer commands and verify live desktop state continuity."""
    client = DesktopObserverClient(executable_path=str(WATCHER_EXE), startup_timeout_seconds=8.0)
    state_mgr = DesktopStateManager()
    client.add_event_listener(state_mgr.handle_event)

    try:
        t0 = time.perf_counter()
        client.start()
        startup_ms = (time.perf_counter() - t0) * 1000.0
        assert startup_ms < 5000.0  # Startup under 5 seconds

        # 1. Measure ping roundtrip latency
        t_ping0 = time.perf_counter()
        pong = client.ping()
        ping_latency_ms = (time.perf_counter() - t_ping0) * 1000.0
        assert pong.get("pong") is True
        assert ping_latency_ms < 200.0  # Ping under 200ms

        # 2. Measure inspection latency on active window
        t_insp0 = time.perf_counter()
        insp = client.inspect(hwnd=0, max_depth=2)
        insp_latency_ms = (time.perf_counter() - t_insp0) * 1000.0
        assert "root" in insp
        assert insp_latency_ms < 1000.0  # Inspection under 1s

        # 3. Measure screenshot latency
        t_snap0 = time.perf_counter()
        snap = client.screenshot(hwnd=0)
        snap_latency_ms = (time.perf_counter() - t_snap0) * 1000.0
        assert "screenshot_base64" in snap
        assert snap_latency_ms < 1500.0  # Screenshot under 1.5s

        # Print live benchmark metrics
        print(f"\n[LIVE BENCHMARK] Startup: {startup_ms:.1f}ms | Ping: {ping_latency_ms:.1f}ms | Inspect: {insp_latency_ms:.1f}ms | Screenshot: {snap_latency_ms:.1f}ms")

    finally:
        client.stop()
        assert client.is_running is False
        assert client.is_running is False
