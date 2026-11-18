"""WorkflowsView — Visualization of multi-step execution workflows and agent collaboration.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
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
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.service import UiService
from src.ui.desktop.theme import (
    ACCENT_AMBER,
    ACCENT_GREEN,
    ACCENT_LAVENDER,
    ACCENT_PURPLE,
    ACCENT_RED,
    BG_CARD,
    BG_MAIN,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class WorkflowCard(QFrame):
    """Card displaying a multi-agent workflow tree or delegation chain."""

    def __init__(self, delegation: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-left: 4px solid {ACCENT_PURPLE}; border-radius: 8px; padding: 12px; margin-bottom: 8px;"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        hdr = QHBoxLayout()
        src = delegation.get("source_agent_id", "Brain")
        dst = delegation.get("destination_agent_id", "Agent")
        title_lbl = QLabel(f"⚡ <b>{src.upper()}</b>  ➔  <b>{dst.upper()}</b>")
        title_lbl.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 13px;")

        status = str(delegation.get("status", "PENDING")).upper()
        if status == "COMPLETED":
            st_color = ACCENT_GREEN
        elif status in ("RUNNING", "PENDING"):
            st_color = ACCENT_AMBER
        else:
            st_color = ACCENT_RED

        status_lbl = QLabel(f"● {status}")
        status_lbl.setStyleSheet(f"color: {st_color}; font-size: 11px; font-weight: 700;")

        hdr.addWidget(title_lbl)
        hdr.addStretch()
        hdr.addWidget(status_lbl)
        layout.addLayout(hdr)

        assign_lbl = QLabel(f"Assignment: {delegation.get('assignment', '')}")
        assign_lbl.setWordWrap(True)
        assign_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        layout.addWidget(assign_lbl)

        res = delegation.get("result")
        if res:
            res_box = QLabel(f"Result: {res}")
            res_box.setWordWrap(True)
            res_box.setStyleSheet(
                f"background-color: {BG_MAIN}; border: 1px solid {BORDER_SUBTLE}; "
                f"border-radius: 4px; padding: 6px; color: {TEXT_PRIMARY}; font-size: 11px;"
            )
            layout.addWidget(res_box)


class WorkflowsView(QWidget):
    """View rendering active and historical multi-agent execution workflows."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        hdr = QHBoxLayout()
        title = QLabel("WORKFLOW EXECUTION & DELEGATION")
        title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1px;")
        hdr.addWidget(title)
        hdr.addStretch()

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setStyleSheet(f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; color: {TEXT_PRIMARY};")
        refresh_btn.clicked.connect(self._service.refresh_workflows)
        hdr.addWidget(refresh_btn)
        layout.addLayout(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._card_layout = QVBoxLayout(self._container)
        self._card_layout.setSpacing(8)
        self._card_layout.addStretch()
        scroll.setWidget(self._container)
        layout.addWidget(scroll)

        self._empty_label = QLabel("No active multi-agent delegations yet.")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 13px; margin-top: 40px;")
        layout.addWidget(self._empty_label)

        self._service.workflows_updated.connect(self.set_workflows)

    def set_workflows(self, workflows: List[Dict[str, Any]]) -> None:
        # Clear existing
        while self._card_layout.count() > 1:
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._empty_label.setVisible(len(workflows) == 0)

        for wf in workflows:
            card = WorkflowCard(wf)
            self._card_layout.insertWidget(self._card_layout.count() - 1, card)
