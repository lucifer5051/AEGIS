"""SidebarWidget — Left navigation panel with Kiro Crew styling and section categorization.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.theme import (
    ACCENT_LAVENDER,
    ACCENT_PURPLE,
    BG_CARD,
    BG_HOVER,
    BG_SIDEBAR,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class SidebarWidget(QFrame):
    """Persistent left-side navigation sidebar for SAGE workstation."""

    nav_changed = Signal(str)  # Target view identifier

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(210)
        self.setStyleSheet(
            f"background-color: {BG_SIDEBAR}; "
            f"border-right: 1px solid {BORDER_SUBTLE};"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 14, 10, 14)
        root.setSpacing(12)

        # Scroll area for navigation links
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        self._nav_layout = QVBoxLayout(container)
        self._nav_layout.setContentsMargins(0, 0, 0, 0)
        self._nav_layout.setSpacing(4)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        # ---------------------------------------------------------------------
        # Primary Navigation
        # ---------------------------------------------------------------------
        self._add_section_header("PRIMARY")

        self.btn_sessions = self._create_nav_button("💬  Sessions", "sessions")
        self.btn_agents = self._create_nav_button("🤖  Agents", "agents")
        self.btn_skills = self._create_nav_button("🧩  Skills", "skills")
        self.btn_models = self._create_nav_button("🎛  Models", "models")
        self.btn_workflows = self._create_nav_button("⚡  Workflows", "workflows")
        self.btn_tasks = self._create_nav_button("📋  Tasks", "tasks")
        self.btn_artifacts = self._create_nav_button("📦  Artifacts", "artifacts")
        self.btn_schedules = self._create_nav_button("⏱  Schedules", "schedules")

        # ---------------------------------------------------------------------
        # System Navigation
        # ---------------------------------------------------------------------
        self._nav_layout.addSpacing(14)
        self._add_section_header("SYSTEM")

        self.btn_memory = self._create_nav_button("🧠  Memory", "memory")
        self.btn_settings = self._create_nav_button("⚙  Settings", "settings")
        self.btn_status = self._create_nav_button("📊  System Status", "status")

        self._nav_layout.addStretch()
        scroll.setWidget(container)
        root.addWidget(scroll)

        # Set default selection
        self.btn_sessions.setChecked(True)

        # Compatibility list widget
        from PySide6.QtWidgets import QListWidget
        self.task_list = QListWidget()

    def _add_section_header(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: 10px; font-weight: 700; "
            f"letter-spacing: 1px; padding: 4px 8px 2px 8px;"
        )
        self._nav_layout.addWidget(lbl)

    def _create_nav_button(self, text: str, view_id: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setProperty("view_id", view_id)
        btn.setFixedHeight(34)
        btn.setStyleSheet(f"""
            QPushButton {{
                text-align: left;
                padding-left: 10px;
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                color: {TEXT_MUTED};
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {BG_HOVER};
                color: {TEXT_PRIMARY};
            }}
            QPushButton:checked {{
                background-color: #2F2147;
                color: {ACCENT_LAVENDER};
                font-weight: 700;
                border-left: 3px solid {ACCENT_PURPLE};
                border-radius: 4px;
            }}
        """)
        btn.clicked.connect(lambda: self.nav_changed.emit(view_id))
        self._button_group.addButton(btn)
        self._nav_layout.addWidget(btn)
        return btn

    def select_view(self, view_id: str) -> None:
        """Programmatically switch active navigation button."""
        for btn in self._button_group.buttons():
            if btn.property("view_id") == view_id:
                btn.setChecked(True)
                break

    def set_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        """Compatibility helper for task count updates."""
        self.task_list.clear()
        for t in tasks:
            from PySide6.QtWidgets import QListWidgetItem
            self.task_list.addItem(QListWidgetItem(t.get("title", "Task")))
        count = len(tasks)
        self.btn_tasks.setText(f"📋  Tasks ({count})" if count else "📋  Tasks")

    def set_status(self, status: Dict[str, Any]) -> None:
        """Compatibility helper for system status updates."""
        pass
