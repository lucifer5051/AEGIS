"""SAGE PySide6 Desktop Application Package.

Canonical Reference:
    PROMPT 079: SAGE Unified Friday Agent & PySide6 Desktop Application
"""

from src.ui.desktop.main_window import SageMainWindow
from src.ui.desktop.service import UiService

__all__ = ["SageMainWindow", "UiService"]
