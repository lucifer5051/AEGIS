"""TasksView — Comprehensive searchable task history and execution details.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.service import UiService
from src.ui.desktop.theme import (
    ACCENT_AMBER,
    ACCENT_GREEN,
    ACCENT_PURPLE,
    ACCENT_RED,
    BG_CARD,
    BG_MAIN,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class TasksView(QWidget):
    """View displaying searchable historical supervisor tasks and states."""

    task_selected = Signal(str)

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        hdr = QHBoxLayout()
        title = QLabel("TASK EXECUTION HISTORY")
        title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1px;")
        hdr.addWidget(title)
        hdr.addStretch()

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter tasks…")
        self.filter_box.setFixedWidth(200)
        self.filter_box.textChanged.connect(self._filter_tasks)
        hdr.addWidget(self.filter_box)

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setStyleSheet(f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; color: {TEXT_PRIMARY};")
        refresh_btn.clicked.connect(self._service.refresh_tasks)
        hdr.addWidget(refresh_btn)
        layout.addLayout(hdr)

        # Table
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Status", "Task Title", "Task ID", "Created At", "Goal"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 6px; color: {TEXT_PRIMARY};"
        )
        self.table.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.table)

        self._all_tasks: List[Dict[str, Any]] = []

        self._service.tasks_updated.connect(self.set_tasks)

    def set_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        self._all_tasks = tasks
        self._render_table(tasks)

    def _render_table(self, tasks: List[Dict[str, Any]]) -> None:
        self.table.setRowCount(0)
        for t in tasks:
            row = self.table.rowCount()
            self.table.insertRow(row)

            status = str(t.get("status", "PENDING")).upper()
            if status in ("SUCCESS", "COMPLETED"):
                st_item = QTableWidgetItem("✔ " + status)
                st_item.setForeground(Qt.GlobalColor.green)
            elif status in ("RUNNING", "IN_PROGRESS"):
                st_item = QTableWidgetItem("● " + status)
                st_item.setForeground(Qt.GlobalColor.yellow)
            elif status in ("FAILED", "ERROR", "CANCELLED"):
                st_item = QTableWidgetItem("✘ " + status)
                st_item.setForeground(Qt.GlobalColor.red)
            else:
                st_item = QTableWidgetItem("○ " + status)
                st_item.setForeground(Qt.GlobalColor.gray)

            title_item = QTableWidgetItem(t.get("title", "Untitled Task"))
            id_item = QTableWidgetItem(t.get("id", "")[:12])
            id_item.setData(Qt.ItemDataRole.UserRole, t.get("id"))
            time_item = QTableWidgetItem(t.get("created_at", "")[:19].replace("T", " "))
            goal_item = QTableWidgetItem(t.get("goal", ""))

            self.table.setItem(row, 0, st_item)
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, id_item)
            self.table.setItem(row, 3, time_item)
            self.table.setItem(row, 4, goal_item)

    def _filter_tasks(self, query: str) -> None:
        q = query.strip().lower()
        if not q:
            self._render_table(self._all_tasks)
            return
        filtered = [
            t for t in self._all_tasks
            if q in t.get("title", "").lower() or q in t.get("goal", "").lower()
        ]
        self._render_table(filtered)

    def _on_item_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        id_item = self.table.item(row, 2)
        if id_item:
            task_id = id_item.data(Qt.ItemDataRole.UserRole)
            if task_id:
                self._service.select_task(task_id)
                self.task_selected.emit(task_id)
