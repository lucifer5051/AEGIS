"""ConfigView — Agent, model routing, and autonomy policy configuration.

Canonical Reference:
    PROMPT 079: SAGE Hybrid Central Brain & PySide6 Desktop Application
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.ui.desktop.service import UiService
from src.ui.desktop.theme import (
    ACCENT_GREEN,
    ACCENT_PURPLE,
    BG_CARD,
    BG_MAIN,
    BORDER_SUBTLE,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


class ConfigView(QWidget):
    """View managing model routing tiers, local-first policies, and agent autonomy."""

    def __init__(self, service: UiService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)

        title = QLabel("MODEL & AGENT CONFIGURATION")
        title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; letter-spacing: 1px;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        container = QWidget()
        c_layout = QVBoxLayout(container)
        c_layout.setSpacing(16)

        # 1. Model Routing Tiers
        m_frame = QFrame()
        m_frame.setStyleSheet(f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; border-radius: 8px; padding: 14px;")
        m_layout = QVBoxLayout(m_frame)
        m_title = QLabel("Model Routing Tiers (ModelGateway)")
        m_title.setStyleSheet(f"font-weight: 700; color: {TEXT_PRIMARY}; font-size: 13px;")
        m_layout.addWidget(m_title)

        grid = QGridLayout()
        grid.addWidget(QLabel("Balanced Tier:"), 0, 0)
        c1 = QComboBox(); c1.addItems(["sage-local-v1", "qwen35-4b-q4km", "gpt-4o"])
        grid.addWidget(c1, 0, 1)

        grid.addWidget(QLabel("Reasoning Tier:"), 1, 0)
        c2 = QComboBox(); c2.addItems(["sage-local-v1", "deepseek-r1", "claude-3-7-sonnet"])
        grid.addWidget(c2, 1, 1)

        grid.addWidget(QLabel("Coding Tier:"), 2, 0)
        c3 = QComboBox(); c3.addItems(["sage-local-v1", "qwen2.5-coder", "gpt-4o"])
        grid.addWidget(c3, 2, 1)

        grid.addWidget(QLabel("WhatsApp Small Model:"), 3, 0)
        c4 = QComboBox(); c4.addItems(["sage-local-v1 (local)", "fast", "mistral-7b"])
        grid.addWidget(c4, 3, 1)

        m_layout.addLayout(grid)
        c_layout.addWidget(m_frame)

        # 2. Security & Autonomy Policy
        p_frame = QFrame()
        p_frame.setStyleSheet(f"background-color: {BG_CARD}; border: 1px solid {BORDER_SUBTLE}; border-radius: 8px; padding: 14px;")
        p_layout = QVBoxLayout(p_frame)
        p_title = QLabel("TrustCore Autonomy & Security Mode")
        p_title.setStyleSheet(f"font-weight: 700; color: {TEXT_PRIMARY}; font-size: 13px;")
        p_layout.addWidget(p_title)

        autonomy_layout = QHBoxLayout()
        autonomy_layout.addWidget(QLabel("Autonomy Level:"))
        auto_combo = QComboBox()
        auto_combo.addItems(["SUPERVISED (Approvals for Medium/High Risk)", "ASSISTED (Manual confirmation)", "AUTONOMOUS (Low risk permitted)"])
        autonomy_layout.addWidget(auto_combo)
        p_layout.addLayout(autonomy_layout)

        chk_local = QCheckBox("Enforce 100% LOCAL_ONLY Network Mode (Strict Offline)")
        chk_local.setChecked(True)
        chk_local.setStyleSheet(f"color: {TEXT_PRIMARY};")
        p_layout.addWidget(chk_local)

        chk_zero_secret = QCheckBox("Enforce Zero-Secret Sanitization across all logs and channels")
        chk_zero_secret.setChecked(True)
        p_layout.addWidget(chk_zero_secret)

        c_layout.addWidget(p_frame)

        c_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)
