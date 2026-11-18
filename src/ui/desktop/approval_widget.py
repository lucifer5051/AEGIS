"""TrustCore approval panel — shows pending approvals and handles approve/reject."""
from __future__ import annotations

from typing import Any, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class _ApprovalRow(QFrame):
    approved = Signal(str)
    rejected = Signal(str)

    def __init__(self, approval: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._id = approval.get("id", "")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("approvalRow")

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)

        action = approval.get("action", approval.get("requested_action", "unknown"))
        risk = approval.get("risk_level", approval.get("risk", "MEDIUM"))
        resource = approval.get("resource", approval.get("target_resource", ""))
        task_id = approval.get("task_id", "")

        title_lbl = QLabel(f"<b>{action}</b>")
        title_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)

        if resource:
            res_lbl = QLabel(f"Resource: {resource}")
            res_lbl.setObjectName("approvalMeta")
            layout.addWidget(res_lbl)

        risk_lbl = QLabel(f"Risk: {risk}")
        risk_lbl.setObjectName("approvalRisk")
        layout.addWidget(risk_lbl)

        if task_id:
            tid_lbl = QLabel(f"Task: {task_id[:12]}…")
            tid_lbl.setObjectName("approvalMeta")
            layout.addWidget(tid_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        approve_btn = QPushButton("✔ Approve")
        approve_btn.setObjectName("approveBtn")
        approve_btn.clicked.connect(lambda: self.approved.emit(self._id))
        btn_row.addWidget(approve_btn)

        reject_btn = QPushButton("✘ Reject")
        reject_btn.setObjectName("rejectBtn")
        reject_btn.clicked.connect(lambda: self.rejected.emit(self._id))
        btn_row.addWidget(reject_btn)

        layout.addLayout(btn_row)


class ApprovalWidget(QFrame):
    """Panel listing pending TrustCore authorization requests."""

    approve_requested = Signal(str)   # approval_id
    reject_requested = Signal(str)    # approval_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("approvalWidget")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(8, 6, 8, 6)
        self._title = QLabel("🔒 APPROVALS")
        self._title.setObjectName("panelTitle")
        header.addWidget(self._title)
        header.addStretch()
        self._badge = QLabel("0")
        self._badge.setObjectName("approvalBadge")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setFixedSize(22, 22)
        header.addWidget(self._badge)
        root.addLayout(header)

        # Scroll area for rows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._container = QWidget()
        self._list_layout = QVBoxLayout(self._container)
        self._list_layout.setSpacing(6)
        self._list_layout.setContentsMargins(6, 4, 6, 4)
        self._list_layout.addStretch()
        scroll.setWidget(self._container)
        root.addWidget(scroll)

        self._empty = QLabel("No pending approvals")
        self._empty.setObjectName("emptyState")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._empty)

        self._rows: List[_ApprovalRow] = []

    # ------------------------------------------------------------------
    def set_approvals(self, approvals: List[Any]) -> None:
        """Replace displayed approvals with *approvals* (list of dicts or objects)."""
        # Clear existing rows
        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        # Normalise to dicts
        items: List[dict] = []
        for a in approvals:
            if isinstance(a, dict):
                items.append(a)
            else:
                # dataclass / pydantic model → __dict__ or model_dump
                try:
                    items.append(a.model_dump())
                except AttributeError:
                    items.append(vars(a))

        count = len(items)
        self._badge.setText(str(count))
        self._empty.setVisible(count == 0)

        for item in items:
            row = _ApprovalRow(item, self._container)
            row.approved.connect(self.approve_requested)
            row.rejected.connect(self.reject_requested)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._rows.append(row)

        self._title.setText(f"🔒 APPROVALS  ({count})" if count else "🔒 APPROVALS")
