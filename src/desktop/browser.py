"""SAGE Desktop Awareness Subsystem -- Chrome Session & Browser Control Foundation.

Canonical Reference:
    PROMPT 077: Real-World Application Compatibility, Semantic UI Interaction & Reliability Hardening

Implements:
- ChromeSessionManager: Window discovery, foreground tracking, and session awareness.
- BrowserTabRegistry: Tab enumeration, active tab tracking, and switch detection.
- WebApplicationDetector: Contextual domain and application identification.
- WebElementRegistry: Ephemeral web element identity and generational tracking.
- BrowserInteractionAdapter: Semantic UIA with OCR fallback for web controls.
- DownloadVerificationAdapter: Bounded, verified filesystem download workflow.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, unique
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.desktop.action_adapter import DesktopActionAdapter
from src.desktop.client import DesktopObserverClient
from src.desktop.element_registry import DesktopElementRegistry, LocalityTuple
from src.desktop.errors import DesktopProcessError, DesktopSecurityError
from src.desktop.vision_fallback import VisionFallbackAdapter
from src.models.errors import redact_secrets_from_text

logger = logging.getLogger(__name__)


@unique
class WebApplicationType(str, Enum):
    """Recognized web applications within Google Chrome."""

    ARXIV = "arxiv"
    GOOGLE_SCHOLAR = "google_scholar"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    RESEARCHGATE = "researchgate"
    YOUTUBE = "youtube"
    WHATSAPP_WEB = "whatsapp_web"
    INSTAGRAM = "instagram"
    REDDIT = "reddit"
    GEMINI = "gemini"
    CLAUDE = "claude"
    NOTION = "notion"
    GOOGLE_CALENDAR = "google_calendar"
    GMAIL = "gmail"
    GOOGLE_DRIVE = "google_drive"
    GENERIC_WEB = "generic_web"


@dataclass
class BrowserTab:
    """Represents an active or background browser tab in Chrome."""

    tab_id: str
    title: str
    url: str = ""
    is_active: bool = False
    hwnd: int = 0
    app_type: WebApplicationType = WebApplicationType.GENERIC_WEB
    last_seen_at: float = field(default_factory=time.time)


class WebApplicationDetector:
    """Heuristic detector mapping browser titles and URLs to recognized web application profiles."""

    APP_SIGNATURES: List[Tuple[WebApplicationType, List[str]]] = [
        (WebApplicationType.ARXIV, ["arxiv.org", "arxiv", "[arxiv]"]),
        (WebApplicationType.GOOGLE_SCHOLAR, ["scholar.google", "google scholar"]),
        (WebApplicationType.SEMANTIC_SCHOLAR, ["semanticscholar.org", "semantic scholar"]),
        (WebApplicationType.RESEARCHGATE, ["researchgate.net", "researchgate"]),
        (WebApplicationType.YOUTUBE, ["youtube.com", "- youtube", "youtube"]),
        (WebApplicationType.WHATSAPP_WEB, ["web.whatsapp.com", "whatsapp web", "(1) whatsapp", "whatsapp"]),
        (WebApplicationType.INSTAGRAM, ["instagram.com", "instagram"]),
        (WebApplicationType.REDDIT, ["reddit.com", "reddit"]),
        (WebApplicationType.GEMINI, ["gemini.google.com", "google gemini", "gemini"]),
        (WebApplicationType.CLAUDE, ["claude.ai", "claude"]),
        (WebApplicationType.NOTION, ["notion.so", "notion"]),
        (WebApplicationType.GOOGLE_CALENDAR, ["calendar.google.com", "google calendar"]),
        (WebApplicationType.GMAIL, ["mail.google.com", "gmail"]),
        (WebApplicationType.GOOGLE_DRIVE, ["drive.google.com", "google drive"]),
    ]

    @classmethod
    def detect(cls, title: str, url: str = "") -> WebApplicationType:
        """Classify page title and URL into a WebApplicationType."""
        combined = f"{title.lower()} {url.lower()}".strip()
        for app_type, patterns in cls.APP_SIGNATURES:
            for pattern in patterns:
                if pattern in combined:
                    return app_type
        return WebApplicationType.GENERIC_WEB


class BrowserTabRegistry:
    """Tracks tabs across Chrome windows, active tab switching, and staleness."""

    def __init__(self) -> None:
        self._tabs: Dict[str, BrowserTab] = {}
        self._active_tab_id: Optional[str] = None

    def register_or_update_tab(
        self,
        tab_id: str,
        title: str,
        url: str = "",
        hwnd: int = 0,
        is_active: bool = True,
    ) -> BrowserTab:
        app_type = WebApplicationDetector.detect(title=title, url=url)
        tab = BrowserTab(
            tab_id=tab_id,
            title=title,
            url=url,
            is_active=is_active,
            hwnd=hwnd,
            app_type=app_type,
            last_seen_at=time.time(),
        )
        if is_active:
            for other in self._tabs.values():
                if other.hwnd == hwnd:
                    other.is_active = False
            self._active_tab_id = tab_id
        self._tabs[tab_id] = tab
        return tab

    def get_active_tab(self) -> Optional[BrowserTab]:
        if self._active_tab_id and self._active_tab_id in self._tabs:
            return self._tabs[self._active_tab_id]
        for tab in self._tabs.values():
            if tab.is_active:
                return tab
        return None

    def list_tabs(self) -> List[BrowserTab]:
        return list(self._tabs.values())

    def remove_tab(self, tab_id: str) -> None:
        self._tabs.pop(tab_id, None)
        if self._active_tab_id == tab_id:
            self._active_tab_id = None


class WebElementRegistry:
    """Tracks ephemeral web UI elements scoped to tab and session generation."""

    def __init__(self, ttl_seconds: float = 45.0) -> None:
        self._ttl = ttl_seconds
        self._elements: Dict[str, Dict[str, Any]] = {}

    def register_element(
        self,
        element_id: str,
        tab_id: str,
        tag: str,
        name: str,
        selector: str,
        session_gen: int = 1,
        bounds: Optional[Dict[str, int]] = None,
    ) -> None:
        self._elements[element_id] = {
            "element_id": element_id,
            "tab_id": tab_id,
            "tag": tag,
            "name": name,
            "selector": selector,
            "session_gen": session_gen,
            "bounds": bounds or {},
            "registered_at": time.time(),
        }

    def is_stale(self, element_id: str, current_tab_id: str, current_session_gen: int) -> Tuple[bool, str]:
        entry = self._elements.get(element_id)
        if not entry:
            return True, f"Element '{element_id}' not found in web registry."
        if entry["session_gen"] != current_session_gen:
            return True, "Observer session generation changed."
        if entry["tab_id"] != current_tab_id:
            return True, f"Active tab changed (element registered for '{entry['tab_id']}', current is '{current_tab_id}')."
        age = time.time() - entry["registered_at"]
        if age > self._ttl:
            return True, f"Element reference expired after {age:.1f}s."
        return False, "Element is fresh."

    def invalidate_tab(self, tab_id: str) -> None:
        self._elements = {k: v for k, v in self._elements.items() if v.get("tab_id") != tab_id}


class ChromeSessionManager:
    """Discovers and inspects active Google Chrome instances."""

    def __init__(
        self,
        client: DesktopObserverClient,
        tab_registry: Optional[BrowserTabRegistry] = None,
        web_registry: Optional[WebElementRegistry] = None,
    ) -> None:
        self._client = client
        self._tab_registry = tab_registry or BrowserTabRegistry()
        self._web_registry = web_registry or WebElementRegistry()

    @property
    def tab_registry(self) -> BrowserTabRegistry:
        return self._tab_registry

    @property
    def web_registry(self) -> WebElementRegistry:
        return self._web_registry

    def discover_chrome_session(self) -> Optional[Dict[str, Any]]:
        """Inspect desktop to discover open Chrome windows and active page state."""
        inspect_res = self._client.inspect(hwnd=0, max_depth=2)
        root = inspect_res.get("root", {})
        title = root.get("Name", "")
        proc_id = root.get("ProcessId", 0)
        hwnd = root.get("NativeWindowHandle", 0)

        # Check if active window is Chrome or search top-level children
        target_node = None
        if "chrome" in title.lower() or "google chrome" in title.lower():
            target_node = root
        else:
            for child in root.get("Children", []):
                c_title = child.get("Name", "")
                if "chrome" in c_title.lower() or "google chrome" in c_title.lower():
                    target_node = child
                    break

        if target_node:
            node_title = target_node.get("Name", "")
            node_hwnd = target_node.get("NativeWindowHandle", hwnd)
            tab_id = f"chrome-tab-{node_hwnd}"
            tab = self._tab_registry.register_or_update_tab(
                tab_id=tab_id,
                title=node_title,
                hwnd=node_hwnd,
                is_active=True,
            )
            return {
                "discovered": True,
                "process_name": "chrome.exe",
                "hwnd": node_hwnd,
                "title": node_title,
                "app_type": tab.app_type.value,
                "active_tab_id": tab.tab_id,
            }

        return None


@dataclass
class DownloadRecord:
    """Verification record of an authorized document download."""

    filename: str
    target_path: Path
    source_url: str
    expected_type: str
    size_bytes: int
    downloaded_at: float = field(default_factory=time.time)
    verified: bool = False
    untrusted_data: bool = True


class DownloadVerificationAdapter:
    """Manages secure, verified downloads into an authorized sandbox directory."""

    def __init__(self, download_dir: Optional[Path] = None) -> None:
        self._download_dir = download_dir or (Path.home() / "Downloads" / "sage_authorized")
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._records: List[DownloadRecord] = []

    @property
    def download_directory(self) -> Path:
        return self._download_dir

    def verify_download(
        self,
        filename: str,
        expected_extension: str,
        source_url: str = "",
        min_bytes: int = 100,
    ) -> DownloadRecord:
        """Verify that a downloaded document exists, matches extension, is readable, and non-empty."""
        clean_name = Path(filename).name
        target_path = self._download_dir / clean_name

        if not str(clean_name).lower().endswith(expected_extension.lower()):
            raise DesktopSecurityError(
                f"Downloaded file '{clean_name}' does not match expected extension '{expected_extension}'"
            )

        if not target_path.exists():
            raise DesktopProcessError(f"File '{clean_name}' was not found in authorized download directory.")

        size = target_path.stat().st_size
        if size < min_bytes:
            raise DesktopProcessError(
                f"File '{clean_name}' has invalid size ({size} bytes, expected >= {min_bytes} bytes)."
            )

        # Confirm readability without executing
        try:
            with open(target_path, "rb") as f:
                _ = f.read(128)
        except Exception as exc:
            raise DesktopProcessError(f"Downloaded file '{clean_name}' could not be read: {exc}") from exc

        rec = DownloadRecord(
            filename=clean_name,
            target_path=target_path,
            source_url=source_url,
            expected_type=expected_extension,
            size_bytes=size,
            verified=True,
            untrusted_data=True,
        )
        self._records.append(rec)
        logger.info("Successfully verified authorized download: %s (%d bytes)", clean_name, size)
        return rec


class BrowserInteractionAdapter:
    """High-level interaction adapter providing semantic web actions with OCR fallback."""

    def __init__(
        self,
        action_adapter: DesktopActionAdapter,
        vision_adapter: VisionFallbackAdapter,
        session_manager: ChromeSessionManager,
    ) -> None:
        self._action = action_adapter
        self._vision = vision_adapter
        self._session = session_manager

    def navigate_url(self, url: str, hwnd: int = 0) -> Dict[str, Any]:
        """Focus the Omnibox via Ctrl+L shortcut and enter a target URL."""
        # Focus address bar using keyboard shortcut
        self._action.shortcut(keys="^l")
        time.sleep(0.1)
        # Type target URL and press Enter
        self._action.type_text(text=f"{url}~")
        time.sleep(0.5)
        return {
            "status": "SUCCESS",
            "action": "navigate_url",
            "url": url,
            "hwnd": hwnd,
            "untrusted_data": True,
        }

    def click_button_or_link(
        self,
        name: str = "",
        automation_id: str = "",
        hwnd: int = 0,
        ocr_fallback: bool = True,
    ) -> Dict[str, Any]:
        """Attempt semantic UIA click first, falling back to OCR coordinates if uninstrumented."""
        # 1. Semantic UIA invocation
        if automation_id or name:
            try:
                res = self._action.click(automation_id=automation_id, name=name)
                return {"status": "SUCCESS", "method": "semantic_uia", "detail": res}
            except Exception as exc:
                logger.debug("Semantic click failed for '%s'/'%s': %s", automation_id, name, exc)

        # 2. Vision & OCR fallback
        if ocr_fallback and name:
            visual = self._vision.inspect_visual(hwnd=hwnd, reason=f"Click target '{name}' lacks UIA")
            click_res = self._vision.click_text(text_query=name, visual_result=visual, hwnd=hwnd)
            return {"status": "SUCCESS", "method": "ocr_coordinate", "detail": click_res}

        raise DesktopProcessError(f"Unable to locate or click web control '{name}' / '{automation_id}'")

    def enter_text(self, text: str, element_id: str = "", hwnd: int = 0) -> Dict[str, Any]:
        """Enter text into a focused or identified web control."""
        if element_id:
            try:
                self._action.set_value(value=text, automation_id=element_id)
                return {"status": "SUCCESS", "method": "value_pattern", "text": redact_secrets_from_text(text)}
            except Exception:
                pass

        # Fallback to direct keyboard typing
        self._action.type_text(text=text)
        return {"status": "SUCCESS", "method": "send_keys", "text": redact_secrets_from_text(text)}
