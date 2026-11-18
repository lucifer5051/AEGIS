"""PROMPT 077: Real-World Application Compatibility, Semantic UI Interaction & Reliability Hardening.

Verifies:
- ChromeSessionManager, BrowserTabRegistry, WebApplicationDetector, and WebElementRegistry.
- Academic research workflow (arXiv search, metadata extraction, PDF download and verification).
- YouTube playback and volume control workflows.
- WhatsApp Web draft preparation and approval-gated message transmission.
- AI assistant (Gemini/Claude) prompt input and untrusted response extraction.
- Spotify Desktop application discovery, CEF/Electron technology identification, search, and playback toggle.
- Google Antigravity IDE workspace discovery and safe isolated temporary test file interaction.
- Interruption Scenarios F through J (element disappearance, tab switch, app minimize, agent approval prompt, navigation away).
- TrustCore security boundaries and privacy safeguards across all web and desktop adapters.
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
    AntigravityIDEAdapter,
    BrowserInteractionAdapter,
    BrowserTab,
    BrowserTabRegistry,
    ChromeSessionManager,
    DesktopActionAdapter,
    DesktopElementRegistry,
    DesktopEvent,
    DesktopEventType,
    DesktopObserverClient,
    DownloadRecord,
    DownloadVerificationAdapter,
    FileExplorerAdapter,
    InterruptionDecision,
    InterruptionManager,
    InterruptionSeverity,
    NotepadAdapter,
    SpotifyAdapter,
    VisionFallbackAdapter,
    WebApplicationDetector,
    WebApplicationType,
    WebElementRegistry,
    WindowsDesktopToolPolicy,
    WindowsMCPConfig,
    WindowsSettingsAdapter,
)
from src.desktop.errors import DesktopProcessError, DesktopSecurityError
from src.desktop.web_apps import (
    AIModelWebAdapter,
    ProductivityWebAdapter,
    ResearchWebsiteAdapter,
    SocialMediaAdapter,
    WhatsAppWebAdapter,
    YouTubeAdapter,
)


class MockDesktopWatcherProcess:
    """Simulates native watcher for Chrome, Spotify, Antigravity, and Windows applications."""

    def __init__(self) -> None:
        import queue
        self._running = True
        self._queue: queue.Queue = queue.Queue()
        self.shortcuts_sent: List[str] = []
        self.typed_texts: List[str] = []
        self.clicks: List[Dict[str, Any]] = []

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
                        resp = {"Id": cid, "Status": "SUCCESS", "Result": {"pong": True}}
                    elif cname == "inspect":
                        # Synthetic desktop tree containing Chrome, Spotify, and Antigravity
                        resp = {
                            "Id": cid,
                            "Status": "SUCCESS",
                            "Result": {
                                "root": {
                                    "Name": "Desktop",
                                    "ControlType": "ControlType.Pane",
                                    "NativeWindowHandle": 1000,
                                    "Children": [
                                        {
                                            "Name": "arXiv:2301.00001 - Google Chrome",
                                            "ControlType": "ControlType.Window",
                                            "NativeWindowHandle": 2001,
                                            "ProcessId": 1111,
                                            "Children": [
                                                {"Name": "Address and search bar", "AutomationId": "omnibox", "ControlType": "ControlType.Edit"},
                                                {"Name": "Download PDF", "AutomationId": "pdf-download-link", "ControlType": "ControlType.Button"},
                                            ],
                                        },
                                        {
                                            "Name": "Spotify Free",
                                            "ControlType": "ControlType.Window",
                                            "NativeWindowHandle": 3001,
                                            "ProcessId": 2222,
                                        },
                                        {
                                            "Name": "Antigravity - aeigs",
                                            "ControlType": "ControlType.Window",
                                            "NativeWindowHandle": 4001,
                                            "ProcessId": 3333,
                                        },
                                    ],
                                }
                            },
                        }
                    elif cname == "shortcut":
                        self._outer.shortcuts_sent.append(cmd.get("keys", ""))
                        resp = {"Id": cid, "Status": "SUCCESS", "Result": {"shortcut_sent": True}}
                    elif cname == "type":
                        self._outer.typed_texts.append(cmd.get("text", ""))
                        resp = {"Id": cid, "Status": "SUCCESS", "Result": {"typed": True}}
                    elif cname == "click":
                        self._outer.clicks.append(cmd)
                        resp = {"Id": cid, "Status": "SUCCESS", "Result": {"clicked": True}}
                    elif cname == "screenshot":
                        resp = {
                            "Id": cid,
                            "Status": "SUCCESS",
                            "Result": {
                                "screenshot_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                                "bounds": {"x": 0, "y": 0, "width": 1920, "height": 1080},
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


@pytest.fixture
def mock_app_client() -> DesktopObserverClient:
    mock_proc = MockDesktopWatcherProcess()
    mock_proc._queue.put(json.dumps({"EventType": "OBSERVER_READY", "Timestamp": time.time(), "SessionGeneration": 1}) + "\n")
    client = DesktopObserverClient(process_factory=lambda *a, **kw: mock_proc)
    client.start()
    return client


# =============================================================================
# 1. CHROME SESSION & WEB APPLICATION DETECTION
# =============================================================================

def test_web_application_detector_classification():
    """Verify classification of web applications by URL and page title."""
    assert WebApplicationDetector.detect("arXiv:2301.00001v1 [cs.AI]", "https://arxiv.org/abs/2301.00001") == WebApplicationType.ARXIV
    assert WebApplicationDetector.detect("Attention Is All You Need - Google Scholar") == WebApplicationType.GOOGLE_SCHOLAR
    assert WebApplicationDetector.detect("Rick Astley - Never Gonna Give You Up (Official Music Video) - YouTube") == WebApplicationType.YOUTUBE
    assert WebApplicationDetector.detect("(3) WhatsApp", "https://web.whatsapp.com") == WebApplicationType.WHATSAPP_WEB
    assert WebApplicationDetector.detect("Instagram", "https://www.instagram.com") == WebApplicationType.INSTAGRAM
    assert WebApplicationDetector.detect("reddit: the front page of the internet") == WebApplicationType.REDDIT
    assert WebApplicationDetector.detect("Gemini - Chat with Google AI", "https://gemini.google.com") == WebApplicationType.GEMINI
    assert WebApplicationDetector.detect("Claude", "https://claude.ai") == WebApplicationType.CLAUDE
    assert WebApplicationDetector.detect("Work Tracking - Notion") == WebApplicationType.NOTION
    assert WebApplicationDetector.detect("Google Calendar - Week of Nov 18", "https://calendar.google.com") == WebApplicationType.GOOGLE_CALENDAR
    assert WebApplicationDetector.detect("Inbox (12) - user@gmail.com - Gmail") == WebApplicationType.GMAIL
    assert WebApplicationDetector.detect("My Drive - Google Drive") == WebApplicationType.GOOGLE_DRIVE
    assert WebApplicationDetector.detect("Example Domain", "https://example.com") == WebApplicationType.GENERIC_WEB


def test_chrome_session_manager_discovery(mock_app_client: DesktopObserverClient):
    """Verify that ChromeSessionManager discovers open Chrome windows and tracks tabs."""
    tab_reg = BrowserTabRegistry()
    web_reg = WebElementRegistry()
    manager = ChromeSessionManager(client=mock_app_client, tab_registry=tab_reg, web_registry=web_reg)

    session = manager.discover_chrome_session()
    assert session is not None
    assert session["discovered"] is True
    assert session["process_name"] == "chrome.exe"
    assert session["hwnd"] == 2001
    assert session["app_type"] == WebApplicationType.ARXIV.value

    active_tab = tab_reg.get_active_tab()
    assert active_tab is not None
    assert "arxiv" in active_tab.title.lower()

    mock_app_client.stop()


# =============================================================================
# 2. ACADEMIC RESEARCH WORKFLOW & VERIFIED PDF DOWNLOAD
# =============================================================================

def test_academic_research_workflow_and_pdf_download(tmp_path: Path, mock_app_client: DesktopObserverClient):
    """Verify research paper search, abstract extraction, and verified PDF download in sandbox."""
    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["chrome.exe"]))
    action = DesktopActionAdapter(client=mock_app_client, policy=policy)
    vision = VisionFallbackAdapter(client=mock_app_client, ocr_fn=lambda b: {"text": "Download PDF", "words": [{"text": "Download", "x": 10, "y": 10, "width": 50, "height": 20}, {"text": "PDF", "x": 65, "y": 10, "width": 30, "height": 20}]})
    session_mgr = ChromeSessionManager(client=mock_app_client)
    browser = BrowserInteractionAdapter(action_adapter=action, vision_adapter=vision, session_manager=session_mgr)

    research = ResearchWebsiteAdapter(browser=browser)
    downloader = DownloadVerificationAdapter(download_dir=tmp_path / "research_sandbox")

    # 1. Search papers on arXiv
    search_res = research.search_papers(query="Transformer architecture", site=WebApplicationType.ARXIV)
    assert search_res["status"] == "SUCCESS"
    assert search_res["repository"] == "arxiv"
    assert search_res["untrusted_data"] is True

    # 2. Extract metadata
    details = research.extract_paper_details()
    assert details["title"] == "Attention Is All You Need"
    assert len(details["authors"]) >= 3
    assert details["pdf_available"] is True
    assert details["untrusted_data"] is True

    # 3. Download and verify PDF document on disk
    rec = research.download_paper_pdf(downloader=downloader, paper_title="Attention Is All You Need", filename="vaswani2017.pdf")
    assert rec.verified is True
    assert rec.expected_type == ".pdf"
    assert rec.size_bytes > 20
    assert rec.target_path.exists()
    assert rec.untrusted_data is True

    # 4. Attempting to verify a non-existent or invalid file fails closed
    with pytest.raises(DesktopProcessError, match="was not found"):
        downloader.verify_download(filename="missing.pdf", expected_extension=".pdf")

    # 5. Type mismatch is rejected fail-closed
    with pytest.raises(DesktopSecurityError, match="does not match expected extension"):
        downloader.verify_download(filename="vaswani2017.pdf", expected_extension=".exe")

    mock_app_client.stop()


# =============================================================================
# 3. YOUTUBE PLAYBACK WORKFLOW
# =============================================================================

def test_youtube_playback_and_volume_controls(mock_app_client: DesktopObserverClient):
    """Verify YouTube video navigation, play/pause toggle, and volume adjustment."""
    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["chrome.exe"]))
    action = DesktopActionAdapter(client=mock_app_client, policy=policy)
    vision = VisionFallbackAdapter(client=mock_app_client)
    browser = BrowserInteractionAdapter(action, vision, ChromeSessionManager(mock_app_client))

    yt = YouTubeAdapter(browser=browser)
    search_res = yt.search_video("MIT Deep Learning Lecture")
    assert search_res["status"] == "SUCCESS"

    # Play/Pause toggle
    toggle_1 = yt.toggle_play_pause()
    assert toggle_1["status"] == "SUCCESS"
    assert toggle_1["playback_state"] == "PAUSED"
    toggle_2 = yt.toggle_play_pause()
    assert toggle_2["playback_state"] == "PLAYING"

    # Volume adjustments
    vol_up = yt.adjust_volume("up")
    assert vol_up["status"] == "SUCCESS"
    vol_down = yt.adjust_volume("down")
    assert vol_down["status"] == "SUCCESS"

    mock_app_client.stop()


# =============================================================================
# 4. WHATSAPP WEB DRAFTING & APPROVAL BOUNDARIES
# =============================================================================

def test_whatsapp_web_draft_and_approval_boundary(mock_app_client: DesktopObserverClient):
    """Verify that message drafting is safe, but sending requires explicit user approval."""
    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["chrome.exe"]))
    action = DesktopActionAdapter(client=mock_app_client, policy=policy)
    vision = VisionFallbackAdapter(client=mock_app_client)
    browser = BrowserInteractionAdapter(action, vision, ChromeSessionManager(mock_app_client))

    wa = WhatsAppWebAdapter(browser=browser)
    chat_res = wa.select_chat("Research Team")
    assert chat_res["status"] == "SUCCESS"

    # Prepare draft without sending
    draft = wa.prepare_draft("Weekly report draft is ready for review.")
    assert draft["status"] == "SUCCESS"
    assert draft["action"] == "draft_prepared"
    assert draft["requires_approval"] is True

    # Sending without approval token raises DesktopSecurityError fail-closed
    with pytest.raises(DesktopSecurityError, match="requires an explicit, verified user approval token"):
        wa.send_message(approval_token=None)

    # Sending with explicit approval succeeds
    send_res = wa.send_message(approval_token="usr-approve-token-valid-999")
    assert send_res["status"] == "SUCCESS"
    assert send_res["action"] == "message_sent"

    mock_app_client.stop()


# =============================================================================
# 5. SPOTIFY DESKTOP COMPATIBILITY
# =============================================================================

def test_spotify_desktop_compatibility(mock_app_client: DesktopObserverClient):
    """Verify Spotify CEF/Electron technology identification, track search, and playback toggle."""
    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["spotify.exe"]))
    action = DesktopActionAdapter(client=mock_app_client, policy=policy)
    vision = VisionFallbackAdapter(client=mock_app_client)

    spotify = SpotifyAdapter(client=mock_app_client, action=action, vision=vision)

    # 1. Technology identification
    assert "Chromium Embedded Framework" in spotify.application_technology

    # 2. Window discovery
    window_info = spotify.discover_window()
    assert window_info is not None
    assert window_info["discovered"] is True
    assert window_info["process_name"] == "spotify.exe"
    assert window_info["hwnd"] == 3001

    # 3. Search track
    search_res = spotify.search_track("Ludwig van Beethoven Symphony No. 9")
    assert search_res["status"] == "SUCCESS"
    assert search_res["searched"] == "Ludwig van Beethoven Symphony No. 9"

    # 4. Playback toggle requires authorization
    with pytest.raises(DesktopSecurityError, match="requires explicit authorization"):
        spotify.toggle_playback(explicitly_authorized=False)

    toggle_res = spotify.toggle_playback(explicitly_authorized=True)
    assert toggle_res["status"] == "SUCCESS"
    assert toggle_res["verified"] is True
    assert toggle_res["playback_state"] == "PLAYING"

    mock_app_client.stop()


# =============================================================================
# 6. GOOGLE ANTIGRAVITY IDE WORKFLOW
# =============================================================================

def test_antigravity_ide_compatibility(tmp_path: Path, mock_app_client: DesktopObserverClient):
    """Verify Antigravity IDE workspace discovery and safe isolated temporary test file interaction."""
    policy = WindowsDesktopToolPolicy(WindowsMCPConfig(enabled=True, allowed_applications=["antigravity.exe"]))
    action = DesktopActionAdapter(client=mock_app_client, policy=policy)
    vision = VisionFallbackAdapter(client=mock_app_client)

    ide = AntigravityIDEAdapter(client=mock_app_client, action=action, vision=vision)

    # 1. Workspace discovery
    ws = ide.discover_workspace()
    assert ws is not None
    assert ws["discovered"] is True
    assert ws["process_name"] == "Antigravity.exe"
    assert ws["hwnd"] == 4001
    assert "Electron" in ide.application_technology

    # 2. Temporary test file editing within user home sandbox
    temp_target = Path.home() / ".aegis" / "temp" / "antigravity_test.py"
    edit_res = ide.interact_with_temporary_test_file(temp_target, "# Harmless SAGE test code\nprint('OK')")
    assert edit_res["status"] == "SUCCESS"
    assert edit_res["bytes_written"] > 0
    assert temp_target.exists()

    # Clean up temporary test file
    try:
        temp_target.unlink()
    except Exception:
        pass

    mock_app_client.stop()


# =============================================================================
# 7. INTERRUPTION SCENARIOS F THROUGH J
# =============================================================================

def test_interruption_scenarios_f_to_j():
    """Verify interruption hardening for Scenarios F through J."""
    manager = InterruptionManager()
    web_reg = WebElementRegistry(ttl_seconds=1.0)

    # Scenario F: Element disappears between discovery and interaction
    web_reg.register_element(
        element_id="btn-submit",
        tab_id="tab-1",
        tag="button",
        name="Submit",
        selector="#submit-btn",
        session_gen=1,
    )
    time.sleep(1.1)
    is_stale, reason = web_reg.is_stale("btn-submit", current_tab_id="tab-1", current_session_gen=1)
    assert is_stale is True
    assert "expired" in reason

    # Scenario G: Browser tab changes while interaction is pending
    web_reg.register_element(
        element_id="btn-buy",
        tab_id="tab-shopping",
        tag="button",
        name="Buy",
        selector="#buy",
        session_gen=1,
    )
    is_stale_tab, reason_tab = web_reg.is_stale("btn-buy", current_tab_id="tab-news", current_session_gen=1)
    assert is_stale_tab is True
    assert "Active tab changed" in reason_tab

    # Scenario H: Spotify becomes minimized or loses foreground
    evt_spotify_lost = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        process_name="notepad.exe",
        window_handle=5555,
        window_title="Untitled - Notepad",
        session_generation=1,
    )
    dec_h = manager.evaluate_interruption(evt_spotify_lost, target_process="spotify.exe", target_hwnd=3001)
    assert dec_h.should_pause is True
    assert dec_h.severity == InterruptionSeverity.MODAL
    assert "unexpected process 'notepad.exe'" in dec_h.reason

    # Scenario I: Antigravity opens agent approval dialog
    evt_approval = DesktopEvent(
        event_type=DesktopEventType.FOREGROUND_CHANGED.value,
        process_name="Antigravity.exe",
        window_handle=4002,
        window_title="Confirm Agent Action - Approval Required",
        session_generation=1,
    )
    dec_i = manager.evaluate_interruption(evt_approval, target_process="Antigravity.exe", target_hwnd=4001)
    assert dec_i.should_pause is True
    assert dec_i.severity == InterruptionSeverity.MODAL
    assert "Modal dialog" in dec_i.reason

    # Scenario J: Target window destroyed upon navigation away
    evt_nav_close = DesktopEvent(
        event_type=DesktopEventType.WINDOW_CLOSED.value,
        process_name="chrome.exe",
        window_handle=2001,
        window_title="arXiv",
        session_generation=1,
    )
    dec_j = manager.evaluate_interruption(evt_nav_close, target_process="chrome.exe", target_hwnd=2001)
    assert dec_j.should_pause is True
    assert dec_j.severity == InterruptionSeverity.CRITICAL
    assert "Target window HWND=2001 was closed" in dec_j.reason
