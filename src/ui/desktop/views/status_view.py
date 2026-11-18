"""StatusView — Real-time health monitoring of all SAGE architectural subsystems.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
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


class ComponentStatusCard(QFrame):
    """Card displaying live status indicator for an individual backend component."""

    def __init__(self, name: str, description: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 8px; padding: 12px;"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        hdr = QHBoxLayout()
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
        hdr.addWidget(name_lbl)
        hdr.addStretch()

        self.status_pill = QLabel("● Checking…")
        self.status_pill.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {TEXT_MUTED};")
        hdr.addWidget(self.status_pill)
        layout.addLayout(hdr)

        desc_lbl = QLabel(description)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet(f"font-size: 11px; color: {TEXT_MUTED};")
        layout.addWidget(desc_lbl)

    def set_status(self, status: str) -> None:
        st = status.lower()
        if st in ("online", "ok", "running"):
            self.status_pill.setText("● Online")
            self.status_pill.setStyleSheet(f"color: {ACCENT_GREEN}; font-weight: 700; font-size: 11px;")
        elif st in ("degraded", "initializing"):
            self.status_pill.setText("● Degraded")
            self.status_pill.setStyleSheet(f"color: {ACCENT_AMBER}; font-weight: 700; font-size: 11px;")
        else:
            self.status_pill.setText("● Offline")
            self.status_pill.setStyleSheet(f"color: {ACCENT_RED}; font-weight: 700; font-size: 11px;")


class StatusView(QWidget):
    """View rendering comprehensive health matrix for all SAGE runtime subsystems."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)

        hdr = QHBoxLayout()
        title = QLabel("SYSTEM HEALTH & SUBSYSTEM MATRIX")
        title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1px;")
        hdr.addWidget(title)
        hdr.addStretch()

        refresh_btn = QPushButton("↻ Refresh Status")
        refresh_btn.setStyleSheet(f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; color: {TEXT_PRIMARY};")
        refresh_btn.clicked.connect(self._service.refresh_status)
        hdr.addWidget(refresh_btn)
        layout.addLayout(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        container = QWidget()
        self.grid = QGridLayout(container)
        self.grid.setSpacing(12)
        scroll.setWidget(container)
        layout.addWidget(scroll)

        self._cards: Dict[str, ComponentStatusCard] = {}
        self._build_cards()

        self._service.status_updated.connect(self.set_statuses)

    def _build_cards(self) -> None:
        components = [
            ("runtime", "SageApplication Runtime", "Core lifecycle controller and service registry"),
            ("central_brain", "Hybrid Central Brain", "Intent routing, model selection and multi-agent coordination"),
            ("friday", "Friday Conversational Agent", "Primary user-facing conversational interface and advisor"),
            ("supervisor", "Supervisor Execution Engine", "Authoritative goal decomposition, planning and execution controller"),
            ("model_gateway", "ModelGateway & Routing", "Deterministic routing and model fallback management"),
            ("memory_engine", "Unified Memory Engine", "Isolated namespaces and hybrid vector/keyword retrieval"),
            ("trustcore", "TrustCore Authorization", "Authoritative capability verification and escalation gate"),
            ("workers", "Background Worker Manager", "Autonomous background job queue and worker scheduling"),
            ("communication_gateway", "Communication Gateway", "Multi-channel adapter management and sidecar routing"),
            ("whatsapp", "WhatsApp Contact Sidecar", "Channel adapter with small-model isolation routing"),
            ("desktop", "Desktop Awareness Subsystem", "Native Windows UIAutomation and visual observer hooks"),
            ("skills_system", "Skills Registry & Sandboxing", "Modular agent capability manifests and execution boundaries"),
            ("local_inference", "Offline Local Inference Engine", "Resident zero-dependency reasoning fallback and quantized GGUF execution"),
        ]

        cols = 2
        for idx, (key, title, desc) in enumerate(components):
            card = ComponentStatusCard(title, desc)
            self._cards[key] = card
            row = idx // cols
            col = idx % cols
            self.grid.addWidget(card, row, col)

    def set_statuses(self, status_dict: Dict[str, Any]) -> None:
        for k, card in self._cards.items():
            st = status_dict.get(k, "offline")
            card.set_status(str(st))
