"""SAGE PySide6 Desktop -- Execution Plan Widget.

Displays the hierarchical execution steps and progress for an active or selected task.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.theme import (
    ACCENT_BLUE,
    ACCENT_GREEN,
    ACCENT_ORANGE,
    ACCENT_RED,
    BG_CARD,
    BG_PANEL,
    BORDER,
    TEXT_BRIGHT,
    TEXT_MAIN,
    TEXT_MUTED,
)


class StepItemWidget(QWidget):
    """Row widget representing a single execution step in the plan."""

    def __init__(self, step_dict: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        state = step_dict.get("state", "PENDING")
        icon_str = "○"
        icon_color = TEXT_MUTED
        if state == "COMPLETED":
            icon_str = "✓"
            icon_color = ACCENT_GREEN
        elif state == "RUNNING":
            icon_str = "●"
            icon_color = ACCENT_BLUE
        elif state == "AWAITING_APPROVAL":
            icon_str = "⚠"
            icon_color = ACCENT_ORANGE
        elif state == "FAILED":
            icon_str = "✗"
            icon_color = ACCENT_RED

        icon_label = QLabel(icon_str)
        icon_label.setStyleSheet(f"color: {icon_color}; font-size: 13px; font-weight: 700;")
        icon_label.setFixedWidth(16)

        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(1)

        name = step_dict.get("name", "Step")
        name_label = QLabel(name)
        name_label.setStyleSheet(f"color: {TEXT_BRIGHT}; font-size: 12px; font-weight: 500;")

        tool = step_dict.get("tool_name", "")
        summary = step_dict.get("output_summary", "")
        detail_text = tool
        if summary:
            detail_text += f" — {summary}"
        detail_label = QLabel(detail_text)
        detail_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; font-family: 'Cascadia Code', monospace;")

        info_layout.addWidget(name_label)
        if detail_text:
            info_layout.addWidget(detail_label)

        layout.addWidget(icon_label, alignment=Qt.AlignTop)
        layout.addLayout(info_layout, stretch=1)


class ExecutionPlanWidget(QFrame):
    """Panel displaying the current or selected task's execution steps."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 6px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Title Bar
        title_bar = QWidget()
        title_bar.setStyleSheet(f"background-color: {BG_PANEL}; border-bottom: 1px solid {BORDER};")
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(10, 8, 10, 8)

        title = QLabel("🗺 EXECUTION PLAN")
        title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px;")

        self.task_sub_label = QLabel("")
        self.task_sub_label.setStyleSheet(f"color: {ACCENT_BLUE}; font-size: 11px;")

        title_layout.addWidget(title)
        title_layout.addSpacing(6)
        title_layout.addWidget(self.task_sub_label)
        title_layout.addStretch()

        layout.addWidget(title_bar)

        # Steps list
        self.steps_list = QListWidget()
        self.steps_list.setStyleSheet(f"background-color: {BG_PANEL}; border: none;")
        layout.addWidget(self.steps_list, stretch=1)

    def set_task_title(self, title: str) -> None:
        self.task_sub_label.setText(f"— {title[:30]}" if title else "")

    def set_steps(self, steps: List[Dict[str, Any]]) -> None:
        self.steps_list.clear()
        for s in steps:
            item = QListWidgetItem(self.steps_list)
            widget = StepItemWidget(s)
            item.setSizeHint(widget.sizeHint())
            self.steps_list.addItem(item)
            self.steps_list.setItemWidget(item, widget)
