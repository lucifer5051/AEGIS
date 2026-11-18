"""ArtifactsView — Management and inspection of generated project outputs and files.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.service import UiService
from src.ui.desktop.theme import (
    ACCENT_CYAN,
    ACCENT_GREEN,
    ACCENT_PURPLE,
    BG_CARD,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_PRIMARY,
)


class ArtifactsView(QWidget):
    """View rendering generated artifacts, reports, documents and research outputs."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        hdr = QHBoxLayout()
        title = QLabel("ARTIFACTS & OUTPUT BROWSER")
        title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1px;")
        hdr.addWidget(title)
        hdr.addStretch()

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setStyleSheet(f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; color: {TEXT_PRIMARY};")
        refresh_btn.clicked.connect(self._service.refresh_artifacts)
        hdr.addWidget(refresh_btn)
        layout.addLayout(hdr)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Size", "Author Agent", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_PRIMARY};"
        )
        layout.addWidget(self.table)

        self._service.artifacts_updated.connect(self.set_artifacts)

    def set_artifacts(self, artifacts: List[Dict[str, Any]]) -> None:
        self.table.setRowCount(0)
        for a in artifacts:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(a.get("name", "Artifact")))
            self.table.setItem(row, 1, QTableWidgetItem(a.get("type", "file")))
            self.table.setItem(row, 2, QTableWidgetItem(a.get("size", "0 KB")))
            self.table.setItem(row, 3, QTableWidgetItem(a.get("agent", "Atlas")))

            st_item = QTableWidgetItem("✔ " + a.get("status", "Ready"))
            st_item.setForeground(Qt.GlobalColor.green)
            self.table.setItem(row, 4, st_item)
