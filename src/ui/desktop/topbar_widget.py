"""TopBarWidget — Global header toolbar with runtime health, search and identity.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from src.ui.desktop.theme import (
    ACCENT_GREEN,
    ACCENT_PURPLE,
    ACCENT_RED,
    BG_CARD,
    BG_INPUT,
    BG_SIDEBAR,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class TopBarWidget(QFrame):
    """Persistent top toolbar for SAGE desktop application."""

    search_submitted = Signal(str)
    settings_requested = Signal()
    notifications_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(48)
        self.setStyleSheet(
            f"background-color: {BG_SIDEBAR}; "
            f"border-bottom: 1px solid {BORDER_SUBTLE};"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(12)

        # 1. Brand Logo & Title
        brand_layout = QHBoxLayout()
        brand_layout.setSpacing(8)

        logo_label = QLabel("⚡")
        logo_label.setStyleSheet(f"font-size: 16px; color: {ACCENT_PURPLE};")
        title_label = QLabel("SAGE")
        title_label.setStyleSheet(
            f"font-size: 14px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1.5px;"
        )
        sub_label = QLabel("Central Brain")
        sub_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {TEXT_MUTED}; margin-top: 2px;"
        )

        brand_layout.addWidget(logo_label)
        brand_layout.addWidget(title_label)
        brand_layout.addWidget(sub_label)
        layout.addLayout(brand_layout)

        # 2. Runtime Health Status Pill
        self.status_pill = QLabel("● Runtime Online")
        self.status_pill.setStyleSheet(
            f"background-color: {BG_CARD}; color: {ACCENT_GREEN}; "
            f"border: 1px solid {BORDER_SUBTLE}; border-radius: 12px; "
            f"padding: 2px 10px; font-size: 11px; font-weight: 600;"
        )
        layout.addWidget(self.status_pill)

        layout.addSpacing(16)

        # 3. Global Search Input
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search sessions, agents, tasks, artifacts (Ctrl+K)…")
        self.search_input.setFixedWidth(340)
        self.search_input.setFixedHeight(30)
        self.search_input.setStyleSheet(
            f"background-color: {BG_INPUT}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_PRIMARY}; padding: 0 10px; font-size: 12px;"
        )
        self.search_input.returnPressed.connect(self._on_search)
        layout.addWidget(self.search_input)

        layout.addStretch()

        # 4. Active Agent Pill
        self.agent_pill = QLabel("🤖 Friday")
        self.agent_pill.setStyleSheet(
            f"background-color: {BG_CARD}; color: {TEXT_PRIMARY}; "
            f"border: 1px solid {ACCENT_PURPLE}; border-radius: 6px; "
            f"padding: 4px 10px; font-size: 12px; font-weight: 600;"
        )
        layout.addWidget(self.agent_pill)

        # 5. Notifications Trigger
        self.notif_btn = QPushButton("🔔")
        self.notif_btn.setFixedSize(30, 30)
        self.notif_btn.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_MUTED}; font-size: 13px;"
        )
        self.notif_btn.clicked.connect(self.notifications_requested.emit)
        layout.addWidget(self.notif_btn)

        # 6. Settings Trigger
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedSize(30, 30)
        self.settings_btn.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_MUTED}; font-size: 13px;"
        )
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.settings_btn)

    def _on_search(self) -> None:
        text = self.search_input.text().strip()
        if text:
            self.search_submitted.emit(text)

    def set_status(self, is_online: bool, label: str = "Runtime Online") -> None:
        color = ACCENT_GREEN if is_online else ACCENT_RED
        self.status_pill.setText(f"● {label}")
        self.status_pill.setStyleSheet(
            f"background-color: {BG_CARD}; color: {color}; "
            f"border: 1px solid {BORDER_SUBTLE}; border-radius: 12px; "
            f"padding: 2px 10px; font-size: 11px; font-weight: 600;"
        )

    def set_active_agent(self, name: str, model_alias: str = "balanced") -> None:
        self.agent_pill.setText(f"🤖 {name} ({model_alias})")
