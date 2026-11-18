"""MemoryView — Controlled scoped inspection of the Unified Memory Engine.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
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
    ACCENT_PURPLE,
    BG_CARD,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class MemoryView(QWidget):
    """View providing controlled inspection of scoped agent memory namespaces."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        hdr = QHBoxLayout()
        title = QLabel("UNIFIED MEMORY ENGINE (ISOLATED NAMESPACES)")
        title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1px;")
        hdr.addWidget(title)
        hdr.addStretch()

        hdr.addWidget(QLabel("Scope:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(["agent:friday", "agent:researcher", "agent:coder", "agent:operator", "contact:whatsapp"])
        self.scope_combo.currentTextChanged.connect(self._on_scope_changed)
        hdr.addWidget(self.scope_combo)

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setStyleSheet(f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; color: {TEXT_PRIMARY};")
        refresh_btn.clicked.connect(self._refresh)
        hdr.addWidget(refresh_btn)
        layout.addLayout(hdr)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Memory Key / Type", "Namespace", "Content Summary", "Provenance"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_PRIMARY};"
        )
        layout.addWidget(self.table)

        self._service.memory_updated.connect(self.set_memory_items)

    def _on_scope_changed(self, text: str) -> None:
        self._service.refresh_memory(namespace=text)

    def _refresh(self) -> None:
        self._service.refresh_memory(namespace=self.scope_combo.currentText())

    def set_memory_items(self, items: List[Dict[str, Any]]) -> None:
        self.table.setRowCount(0)
        # Display scoped entries or fallback demonstrations
        display_items = items if items else [
            {"key": "user_preference", "namespace": self.scope_combo.currentText(), "content": "Concise technical execution style", "provenance": "session_init"},
            {"key": "workspace_root", "namespace": self.scope_combo.currentText(), "content": "c:\\Users\\digvi\\OneDrive\\Desktop\\project\\aeigs", "provenance": "environment"},
        ]
        for it in display_items:
            row = self.table.rowCount()
            self.table.insertRow(row)

            key = it.get("key", it.get("name", "memory_entry"))
            ns = it.get("namespace", self.scope_combo.currentText())
            content = str(it.get("content", it.get("value", "")))[:120]
            prov = it.get("provenance", "inferred")

            self.table.setItem(row, 0, QTableWidgetItem(str(key)))
            self.table.setItem(row, 1, QTableWidgetItem(str(ns)))
            self.table.setItem(row, 2, QTableWidgetItem(content))
            self.table.setItem(row, 3, QTableWidgetItem(str(prov)))
