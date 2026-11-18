"""SAGE PySide6 Desktop -- Live Activity Widget.

Streams execution telemetry, tool invocations, and supervisor events in real-time.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.theme import (
    ACCENT_BLUE,
    ACCENT_GREEN,
    ACCENT_ORANGE,
    ACCENT_PURPLE,
    ACCENT_YELLOW,
    BG_PANEL,
    BORDER,
    TEXT_BRIGHT,
    TEXT_MAIN,
    TEXT_MUTED,
)


class ActivityItemWidget(QWidget):
    """Row representing a single live activity event."""

    def __init__(self, event_dict: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        ts = event_dict.get("timestamp", "")[11:19]
        ts_lbl = QLabel(ts)
        ts_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; font-family: 'Cascadia Code', monospace;")
        ts_lbl.setFixedWidth(52)

        ev_type = event_dict.get("event_type", "").split(".")[-1].upper()
        type_color = ACCENT_BLUE
        if "TOOL" in ev_type:
            type_color = ACCENT_YELLOW
        elif "STEP" in ev_type:
            type_color = ACCENT_PURPLE
        elif "APPROVAL" in ev_type:
            type_color = ACCENT_ORANGE
        elif "COMPLETED" in ev_type:
            type_color = ACCENT_GREEN

        type_lbl = QLabel(ev_type[:8])
        type_lbl.setStyleSheet(f"color: {type_color}; font-size: 10px; font-weight: 700; font-family: 'Cascadia Code', monospace;")
        type_lbl.setFixedWidth(64)

        msg = event_dict.get("message", "")
        msg_lbl = QLabel(msg)
        msg_lbl.setStyleSheet(f"color: {TEXT_MAIN}; font-size: 11px;")
        msg_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout.addWidget(ts_lbl)
        layout.addWidget(type_lbl)
        layout.addWidget(msg_lbl, stretch=1)


class LiveActivityWidget(QFrame):
    """Panel rendering the real-time execution event stream."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 6px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Title bar
        title_bar = QWidget()
        title_bar.setStyleSheet(f"background-color: {BG_PANEL}; border-bottom: 1px solid {BORDER};")
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(10, 6, 10, 6)

        title = QLabel("📡 LIVE ACTIVITY")
        title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px;")

        self.scroll_lock_btn = QPushButton("🔒 Lock")
        self.scroll_lock_btn.setProperty("class", "btn-small")
        self.scroll_lock_btn.setCheckable(True)
        self.scroll_lock_btn.clicked.connect(self._toggle_lock)

        clear_btn = QPushButton("Clear")
        clear_btn.setProperty("class", "btn-small")
        clear_btn.clicked.connect(self._clear)

        title_layout.addWidget(title)
        title_layout.addStretch()
        title_layout.addWidget(clear_btn)
        title_layout.addWidget(self.scroll_lock_btn)

        layout.addWidget(title_bar)

        # Activity list
        self.activity_list = QListWidget()
        self.activity_list.setStyleSheet(f"background-color: {BG_PANEL}; border: none;")
        layout.addWidget(self.activity_list, stretch=1)

    def add_event(self, event_dict: Dict[str, Any]) -> None:
        item = QListWidgetItem(self.activity_list)
        widget = ActivityItemWidget(event_dict)
        item.setSizeHint(widget.sizeHint())
        self.activity_list.addItem(item)
        self.activity_list.setItemWidget(item, widget)

        # Cap length to prevent unbounded memory growth
        if self.activity_list.count() > 300:
            take_item = self.activity_list.takeItem(0)
            del take_item

        if not self.scroll_lock_btn.isChecked():
            self.activity_list.scrollToBottom()

    def _toggle_lock(self, checked: bool) -> None:
        self.scroll_lock_btn.setText("🔓 Unlock" if checked else "🔒 Lock")

    def _clear(self) -> None:
        self.activity_list.clear()
