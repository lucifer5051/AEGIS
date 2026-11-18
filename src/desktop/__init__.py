"""SAGE Windows Desktop Awareness & Control Subsystem.

Canonical Reference:
    PROMPT 074A / PROMPT 075: SAGE Desktop Awareness Subsystem

Components:
- DesktopObserverClient: Native observer process manager & IPC transport
- DesktopStateManager: Authoritative state tracker & event coalescer
- DifferentialStateTracker: Structural and semantic delta computer
- DesktopElementRegistry: Locality tuples and stale reference detector
- InterruptionManager: Interruption classification (critical, modal, transient)
- TaskContinuityManager: Checkpointing and safe task resumption
- DesktopActionAdapter: Perimeter-checked semantic UIA action dispatcher
- DesktopVerificationService: Post-action UI assertion engine
- VisionFallbackAdapter: Targeted screenshot & OCR fallback
- WindowsDesktopToolPolicy: Boundary enforcer & application allowlisting
"""

from src.desktop.action_adapter import DesktopActionAdapter
from src.desktop.apps import (
    AntigravityIDEAdapter,
    FileExplorerAdapter,
    NotepadAdapter,
    SpotifyAdapter,
    WindowsSettingsAdapter,
)
from src.desktop.browser import (
    BrowserInteractionAdapter,
    BrowserTab,
    BrowserTabRegistry,
    ChromeSessionManager,
    DownloadRecord,
    DownloadVerificationAdapter,
    WebApplicationDetector,
    WebApplicationType,
    WebElementRegistry,
)
from src.desktop.client import DesktopObserverClient
from src.desktop.config import WindowsMCPConfig
from src.desktop.continuity import DesktopTaskCheckpoint, TaskContinuityManager
from src.desktop.differential_tracker import DifferentialStateTracker
from src.desktop.element_registry import DesktopElementRegistry, LocalityTuple
from src.desktop.errors import (
    DesktopAutomationError,
    DesktopProcessError,
    DesktopSecurityError,
    DesktopTimeoutError,
    ToolManifestValidationError,
)
from src.desktop.events import DesktopEvent, DesktopEventType, EventBounds, UIElementNode
from src.desktop.interruption_manager import (
    InterruptionDecision,
    InterruptionManager,
    InterruptionSeverity,
)
from src.desktop.policy import WindowsDesktopToolPolicy
from src.desktop.state_manager import DesktopStateManager, ElementState, WindowState
from src.desktop.verification import DesktopVerificationService, VerificationResult
from src.desktop.vision_fallback import VisionFallbackAdapter
from src.desktop.web_apps import (
    AIModelWebAdapter,
    ProductivityWebAdapter,
    ResearchWebsiteAdapter,
    SocialMediaAdapter,
    WhatsAppWebAdapter,
    YouTubeAdapter,
)

__all__ = [
    "DesktopObserverClient",
    "DesktopStateManager",
    "WindowState",
    "ElementState",
    "DifferentialStateTracker",
    "DesktopElementRegistry",
    "LocalityTuple",
    "InterruptionManager",
    "InterruptionSeverity",
    "InterruptionDecision",
    "TaskContinuityManager",
    "DesktopTaskCheckpoint",
    "DesktopActionAdapter",
    "DesktopVerificationService",
    "VerificationResult",
    "VisionFallbackAdapter",
    "WindowsMCPConfig",
    "WindowsDesktopToolPolicy",
    "DesktopEvent",
    "DesktopEventType",
    "EventBounds",
    "UIElementNode",
    "DesktopAutomationError",
    "DesktopSecurityError",
    "DesktopTimeoutError",
    "DesktopProcessError",
    "ToolManifestValidationError",
    "BrowserTab",
    "BrowserTabRegistry",
    "WebApplicationDetector",
    "WebApplicationType",
    "WebElementRegistry",
    "ChromeSessionManager",
    "DownloadRecord",
    "DownloadVerificationAdapter",
    "BrowserInteractionAdapter",
    "ResearchWebsiteAdapter",
    "YouTubeAdapter",
    "WhatsAppWebAdapter",
    "SocialMediaAdapter",
    "AIModelWebAdapter",
    "ProductivityWebAdapter",
    "SpotifyAdapter",
    "AntigravityIDEAdapter",
    "WindowsSettingsAdapter",
    "FileExplorerAdapter",
    "NotepadAdapter",
]
